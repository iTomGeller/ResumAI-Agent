package com.resumai.agent.config;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import javax.sql.DataSource;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.core.io.Resource;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.stereotype.Component;

/**
 * Versioned, guard-aware schema upgrader for the existing production volume.
 *
 * <p>MySQL 8.0 lacks {@code ADD COLUMN IF NOT EXISTS}; each mutating statement
 * in {@code db/migrations/V*.sql} is preceded by a guard directive
 * ({@code -- @guard column:t.c | table:t | index:t.i}) so migrations are
 * re-runnable and safe on both legacy volumes and freshly bootstrapped
 * schema.sql databases. Applied versions are recorded in
 * {@code schema_migration}. No table or data is ever dropped here.</p>
 */
@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class DbMigrationRunner implements org.springframework.beans.factory.InitializingBean {

    private static final Logger log = LoggerFactory.getLogger(DbMigrationRunner.class);
    private static final Pattern GUARD = Pattern.compile(
            "^--\\s*@guard\\s+(table|column|index):([\\w`]+)(?:\\.([\\w`]+))?\\s*$");

    private final DataSource dataSource;

    public DbMigrationRunner(DataSource dataSource) {
        this.dataSource = dataSource;
    }

    @Override
    public void afterPropertiesSet() throws Exception {
        try (Connection conn = dataSource.getConnection()) {
            ensureMigrationTable(conn);
            Set<String> applied = loadApplied(conn);
            List<Resource> files = discoverMigrations();
            for (Resource file : files) {
                String version = versionOf(file.getFilename());
                String content = read(file);
                if (applied.contains(version)) {
                    continue;
                }
                log.info("Applying schema migration {}", file.getFilename());
                applyMigration(conn, version, file.getFilename(), content);
            }
        }
    }

    private void ensureMigrationTable(Connection conn) throws Exception {
        try (Statement st = conn.createStatement()) {
            st.execute("CREATE TABLE IF NOT EXISTS `schema_migration` ("
                    + "`version` VARCHAR(32) NOT NULL PRIMARY KEY,"
                    + "`filename` VARCHAR(256) NOT NULL,"
                    + "`checksum` VARCHAR(64) NOT NULL,"
                    + "`applied_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                    + "`execution_ms` BIGINT NULL"
                    + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4");
        }
    }

    private Set<String> loadApplied(Connection conn) throws Exception {
        Set<String> versions = new HashSet<>();
        try (Statement st = conn.createStatement();
             ResultSet rs = st.executeQuery("SELECT version FROM schema_migration")) {
            while (rs.next()) {
                versions.add(rs.getString(1));
            }
        }
        return versions;
    }

    private List<Resource> discoverMigrations() throws Exception {
        Resource[] resources = new PathMatchingResourcePatternResolver()
                .getResources("classpath*:db/migrations/V*.sql");
        List<Resource> sorted = new ArrayList<>(List.of(resources));
        sorted.sort((a, b) -> {
            int va = Integer.parseInt(versionOf(a.getFilename()).substring(1));
            int vb = Integer.parseInt(versionOf(b.getFilename()).substring(1));
            return Integer.compare(va, vb);
        });
        return sorted;
    }

    static String versionOf(String filename) {
        int idx = filename.indexOf("__");
        return idx > 0 ? filename.substring(0, idx) : filename.replace(".sql", "");
    }

    private void applyMigration(Connection conn, String version, String filename, String content)
            throws Exception {
        long start = System.currentTimeMillis();
        for (GuardedStatement statement : parse(content)) {
            if (statement.guard != null && guardSatisfied(conn, statement.guard)) {
                log.debug("skip guarded statement, {} already present", statement.guard);
                continue;
            }
            try (Statement st = conn.createStatement()) {
                st.execute(statement.sql);
            } catch (Exception e) {
                throw new IllegalStateException(
                        "Migration " + filename + " failed at statement: "
                                + statement.sql.substring(0, Math.min(160, statement.sql.length()))
                                + " -> " + e.getMessage(), e);
            }
        }
        try (PreparedStatement ps = conn.prepareStatement(
                "INSERT INTO schema_migration(version, filename, checksum, execution_ms) VALUES (?,?,?,?)")) {
            ps.setString(1, version);
            ps.setString(2, filename);
            ps.setString(3, sha256(content));
            ps.setLong(4, System.currentTimeMillis() - start);
            ps.executeUpdate();
        }
        log.info("Schema migration {} applied in {} ms", filename, System.currentTimeMillis() - start);
    }

    record Guard(String kind, String table, String detail) {
        @Override
        public String toString() {
            return kind + ":" + table + (detail != null ? "." + detail : "");
        }
    }

    record GuardedStatement(Guard guard, String sql) {
    }

    static List<GuardedStatement> parse(String content) {
        List<GuardedStatement> statements = new ArrayList<>();
        Guard pendingGuard = null;
        StringBuilder current = new StringBuilder();
        for (String rawLine : content.split("\n")) {
            String line = rawLine.replace("\r", "");
            String trimmed = line.trim();
            Matcher guardMatch = GUARD.matcher(trimmed);
            if (guardMatch.matches()) {
                pendingGuard = new Guard(
                        guardMatch.group(1),
                        guardMatch.group(2).replace("`", ""),
                        guardMatch.group(3) != null ? guardMatch.group(3).replace("`", "") : null);
                continue;
            }
            if (trimmed.startsWith("--") || trimmed.isEmpty() && current.length() == 0) {
                continue;
            }
            current.append(line).append('\n');
            if (trimmed.endsWith(";")) {
                String sql = current.toString().trim();
                sql = sql.substring(0, sql.length() - 1);
                if (!sql.isBlank()) {
                    statements.add(new GuardedStatement(pendingGuard, sql));
                }
                current.setLength(0);
                pendingGuard = null;
            }
        }
        return statements;
    }

    private boolean guardSatisfied(Connection conn, Guard guard) throws Exception {
        String sql = switch (guard.kind()) {
            case "table" -> "SELECT COUNT(*) FROM information_schema.tables "
                    + "WHERE table_schema = DATABASE() AND table_name = ?";
            case "column" -> "SELECT COUNT(*) FROM information_schema.columns "
                    + "WHERE table_schema = DATABASE() AND table_name = ? AND column_name = ?";
            case "index" -> "SELECT COUNT(*) FROM information_schema.statistics "
                    + "WHERE table_schema = DATABASE() AND table_name = ? AND index_name = ?";
            default -> throw new IllegalArgumentException("unknown guard kind: " + guard.kind());
        };
        try (PreparedStatement ps = conn.prepareStatement(sql)) {
            ps.setString(1, guard.table());
            if (!"table".equals(guard.kind())) {
                ps.setString(2, guard.detail());
            }
            try (ResultSet rs = ps.executeQuery()) {
                return rs.next() && rs.getLong(1) > 0;
            }
        }
    }

    private static String read(Resource resource) throws Exception {
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(resource.getInputStream(), StandardCharsets.UTF_8))) {
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                sb.append(line).append('\n');
            }
            return sb.toString();
        }
    }

    private static String sha256(String content) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] hash = digest.digest(content.getBytes(StandardCharsets.UTF_8));
        StringBuilder hex = new StringBuilder();
        for (byte b : hash) {
            hex.append(String.format("%02x", b));
        }
        return hex.toString();
    }
}
