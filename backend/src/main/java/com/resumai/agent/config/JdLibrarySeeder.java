package com.resumai.agent.config;

import com.resumai.agent.service.JdRagService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.stereotype.Component;

/**
 * 应用启动时确保默认 JD 已落库并索引，不依赖前端 localStorage 触发。
 */
@Component
public class JdLibrarySeeder implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(JdLibrarySeeder.class);

    private final JdRagService jdRagService;

    public JdLibrarySeeder(JdRagService jdRagService) {
        this.jdRagService = jdRagService;
    }

    @Override
    public void run(ApplicationArguments args) {
        int indexed = jdRagService.ensureDefaultJdsSeeded();
        log.info("JD library seed complete, indexed {} default JD(s), total in DB: {}",
                indexed, jdRagService.getIndexedJdCount());
    }
}
