package com.resumai.agent.config;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import org.junit.jupiter.api.Test;

class DbMigrationRunnerTest {

    @Test
    void parsesGuardDirectivesAndStatements() {
        String sql = """
                -- comment line
                -- @guard column:resume_task.resume_text
                ALTER TABLE `resume_task` ADD COLUMN `resume_text` MEDIUMTEXT NULL;

                -- @guard table:agent_run
                CREATE TABLE IF NOT EXISTS `agent_run` (
                  `run_id` VARCHAR(64) NOT NULL PRIMARY KEY
                );

                INSERT INTO app_user (id) VALUES ('demo-hr');
                """;
        List<DbMigrationRunner.GuardedStatement> statements = DbMigrationRunner.parse(sql);
        assertEquals(3, statements.size());

        assertEquals("column", statements.get(0).guard().kind());
        assertEquals("resume_task", statements.get(0).guard().table());
        assertEquals("resume_text", statements.get(0).guard().detail());
        assertTrue(statements.get(0).sql().startsWith("ALTER TABLE"));

        assertEquals("table", statements.get(1).guard().kind());
        assertEquals("agent_run", statements.get(1).guard().table());
        assertTrue(statements.get(1).sql().contains("CREATE TABLE IF NOT EXISTS"));

        assertNull(statements.get(2).guard(), "guard must not leak to later statements");
    }

    @Test
    void multiLineStatementsKeepInternalSemicolonsOutOfSplit() {
        String sql = """
                -- @guard index:resume_task.uk_x
                ALTER TABLE `resume_task`
                  ADD UNIQUE KEY `uk_x` (`a`, `b`);
                """;
        List<DbMigrationRunner.GuardedStatement> statements = DbMigrationRunner.parse(sql);
        assertEquals(1, statements.size());
        assertTrue(statements.get(0).sql().contains("ADD UNIQUE KEY"));
        assertEquals("index", statements.get(0).guard().kind());
    }

    @Test
    void versionExtraction() {
        assertEquals("V5", DbMigrationRunner.versionOf("V5__conversation_revisions.sql"));
        assertEquals("V12", DbMigrationRunner.versionOf("V12__later.sql"));
    }
}
