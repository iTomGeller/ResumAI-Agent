package com.resumai.agent.config;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import jakarta.annotation.PostConstruct;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 周期性读取 information_schema.tables，暴露核心表行数与容量 Gauge。
 */
@Component
public class MysqlTableMetricsCollector {

    private static final Logger log = LoggerFactory.getLogger(MysqlTableMetricsCollector.class);

    private final JdbcTemplate jdbcTemplate;
    private final MeterRegistry registry;
    private final MysqlObservabilityProperties properties;

    private final Map<String, TableSnapshot> snapshots = new ConcurrentHashMap<>();

    public MysqlTableMetricsCollector(
            JdbcTemplate jdbcTemplate,
            MeterRegistry registry,
            MysqlObservabilityProperties properties) {
        this.jdbcTemplate = jdbcTemplate;
        this.registry = registry;
        this.properties = properties;
    }

    @PostConstruct
    void init() {
        if (!properties.isEnabled()) {
            return;
        }
        for (String table : properties.getMonitoredTables()) {
            String normalized = table.toLowerCase(Locale.ROOT);
            snapshots.put(normalized, registerTableGauges(normalized));
        }
        refreshTableMetrics();
    }

    @Scheduled(fixedDelayString = "${resumai.mysql.observability.table-refresh-interval-ms:60000}")
    public void refreshTableMetrics() {
        if (!properties.isEnabled()) {
            return;
        }
        List<String> tables = properties.getMonitoredTables();
        if (tables == null || tables.isEmpty()) {
            return;
        }

        String placeholders = String.join(",", tables.stream().map(t -> "?").toList());
        String sql = """
                SELECT table_name, table_rows, data_length, index_length, data_free
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name IN (%s)
                """.formatted(placeholders);

        try {
            List<Map<String, Object>> rows = jdbcTemplate.queryForList(sql, tables.toArray());
            for (Map<String, Object> row : rows) {
                String tableName = String.valueOf(rowValue(row, "table_name")).toLowerCase(Locale.ROOT);
                TableSnapshot snapshot = snapshots.get(tableName);
                if (snapshot == null) {
                    continue;
                }
                snapshot.rows.set(asLong(rowValue(row, "table_rows")));
                long dataBytes = asLong(rowValue(row, "data_length"));
                long indexBytes = asLong(rowValue(row, "index_length"));
                snapshot.dataBytes.set(dataBytes);
                snapshot.indexBytes.set(indexBytes);
                snapshot.totalBytes.set(dataBytes + indexBytes);
                snapshot.freeBytes.set(asLong(rowValue(row, "data_free")));
            }
        } catch (Exception ex) {
            log.warn("刷新 MySQL 表容量指标失败: {}", ex.getMessage());
            Counter.builder("resumai.mysql.table.refresh.error")
                    .description("MySQL 表容量刷新失败次数")
                    .register(registry)
                    .increment();
        }
    }

    private TableSnapshot registerTableGauges(String table) {
        String normalized = table.toLowerCase(Locale.ROOT);
        String tableCn = MysqlMetricLabels.tableCn(normalized);
        String businessCategoryCn = MysqlMetricLabels.businessCategoryCn(normalized, null);
        TableSnapshot snapshot = new TableSnapshot();

        Gauge.builder("resumai.mysql.table.rows", snapshot.rows, AtomicLong::get)
                .description("MySQL 表行数（估算）")
                .tag("table", normalized)
                .tag("table_cn", tableCn)
                .tag("business_category_cn", businessCategoryCn)
                .register(registry);
        Gauge.builder("resumai.mysql.table.data.bytes", snapshot.dataBytes, AtomicLong::get)
                .description("MySQL 表数据大小（字节）")
                .tag("table", normalized)
                .tag("table_cn", tableCn)
                .tag("business_category_cn", businessCategoryCn)
                .register(registry);
        Gauge.builder("resumai.mysql.table.index.bytes", snapshot.indexBytes, AtomicLong::get)
                .description("MySQL 表索引大小（字节）")
                .tag("table", normalized)
                .tag("table_cn", tableCn)
                .tag("business_category_cn", businessCategoryCn)
                .register(registry);
        Gauge.builder("resumai.mysql.table.total.bytes", snapshot.totalBytes, AtomicLong::get)
                .description("MySQL 表总大小（字节）")
                .tag("table", normalized)
                .tag("table_cn", tableCn)
                .tag("business_category_cn", businessCategoryCn)
                .register(registry);
        Gauge.builder("resumai.mysql.table.free.bytes", snapshot.freeBytes, AtomicLong::get)
                .description("MySQL 表碎片空间（字节）")
                .tag("table", normalized)
                .tag("table_cn", tableCn)
                .tag("business_category_cn", businessCategoryCn)
                .register(registry);

        return snapshot;
    }

    private static Object rowValue(Map<String, Object> row, String key) {
        if (row.containsKey(key)) {
            return row.get(key);
        }
        for (Map.Entry<String, Object> entry : row.entrySet()) {
            if (entry.getKey().equalsIgnoreCase(key)) {
                return entry.getValue();
            }
        }
        return null;
    }

    private static long asLong(Object value) {
        if (value == null) {
            return 0L;
        }
        if (value instanceof Number number) {
            return number.longValue();
        }
        try {
            return Long.parseLong(String.valueOf(value));
        } catch (NumberFormatException ex) {
            return 0L;
        }
    }

    private static final class TableSnapshot {
        private final AtomicLong rows = new AtomicLong(0);
        private final AtomicLong dataBytes = new AtomicLong(0);
        private final AtomicLong indexBytes = new AtomicLong(0);
        private final AtomicLong totalBytes = new AtomicLong(0);
        private final AtomicLong freeBytes = new AtomicLong(0);
    }
}
