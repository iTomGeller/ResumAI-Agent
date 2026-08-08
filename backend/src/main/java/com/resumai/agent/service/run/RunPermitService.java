package com.resumai.agent.service.run;

import com.resumai.agent.config.AgentRunProperties;
import jakarta.annotation.PostConstruct;
import java.util.concurrent.TimeUnit;
import org.redisson.api.RPermitExpirableSemaphore;
import org.redisson.api.RedissonClient;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Distributed concurrency control backed by Redis expirable semaphores.
 *
 * <p>The global semaphore bounds workflows executing outside LLM waits. A Run
 * releases it only when all parallel branches are suspended and reacquires it
 * before any returned result continues. The per-conversation semaphore still
 * serializes the complete workflow. Expirable leases prevent crash leaks.</p>
 */
@Service
public class RunPermitService {

    private static final Logger log = LoggerFactory.getLogger(RunPermitService.class);
    private static final String GLOBAL_KEY = "resumai:run:permits:global";
    private static final String CONV_KEY_PREFIX = "resumai:run:permits:conv:";

    private final RedissonClient redisson;
    private final AgentRunProperties properties;

    public RunPermitService(RedissonClient redisson, AgentRunProperties properties) {
        this.redisson = redisson;
        this.properties = properties;
    }

    @PostConstruct
    public void initializeGlobalCapacity() {
        RPermitExpirableSemaphore semaphore =
                redisson.getPermitExpirableSemaphore(GLOBAL_KEY);
        int desired = Math.max(1, properties.getMaxGlobalConcurrent());
        if (!semaphore.trySetPermits(desired)
                && semaphore.getPermits() != desired) {
            // trySetPermits initializes only a missing key. Reconcile an
            // existing semaphore so a measured capacity change takes effect
            // after deployment without deleting active permit leases.
            semaphore.setPermits(desired);
        }
        log.info("workflow execution capacity={} available={} acquired={}",
                semaphore.getPermits(), semaphore.availablePermits(),
                semaphore.acquiredPermits());
    }

    private RPermitExpirableSemaphore globalSemaphore() {
        RPermitExpirableSemaphore semaphore = redisson.getPermitExpirableSemaphore(GLOBAL_KEY);
        return semaphore;
    }

    private RPermitExpirableSemaphore conversationSemaphore(String conversationId) {
        RPermitExpirableSemaphore semaphore =
                redisson.getPermitExpirableSemaphore(CONV_KEY_PREFIX + conversationId);
        semaphore.trySetPermits(1);
        return semaphore;
    }

    /** @return permit id, or null when no permit is available right now. */
    public String tryAcquireGlobal() {
        try {
            return globalSemaphore().tryAcquire(
                    0, leaseMillis(), TimeUnit.MILLISECONDS);
        } catch (Exception e) {
            log.warn("global permit acquire failed: {}", e.getMessage());
            return null;
        }
    }

    /** @return permit id, or null when the conversation is already running a run. */
    public String tryAcquireConversation(String conversationId) {
        try {
            return conversationSemaphore(conversationId).tryAcquire(
                    0, leaseMillis(), TimeUnit.MILLISECONDS);
        } catch (Exception e) {
            log.warn("conversation permit acquire failed conv={}: {}", conversationId, e.getMessage());
            return null;
        }
    }

    public void releaseGlobal(String permitId) {
        if (!StringUtils.hasText(permitId)) {
            return;
        }
        try {
            globalSemaphore().tryRelease(permitId);
        } catch (Exception e) {
            log.debug("global permit release skipped ({}): {}", permitId, e.getMessage());
        }
    }

    public void releaseConversation(String conversationId, String permitId) {
        if (!StringUtils.hasText(permitId) || !StringUtils.hasText(conversationId)) {
            return;
        }
        try {
            conversationSemaphore(conversationId).tryRelease(permitId);
        } catch (Exception e) {
            log.debug("conversation permit release skipped conv={} ({}): {}",
                    conversationId, permitId, e.getMessage());
        }
    }

    /** Renew leases for a healthy in-flight run so long tasks outlive the TTL. */
    public void renewLeases(String conversationId, String convPermitId, String globalPermitId) {
        try {
            if (StringUtils.hasText(globalPermitId)) {
                globalSemaphore().updateLeaseTime(globalPermitId, leaseMillis(), TimeUnit.MILLISECONDS);
            }
            if (StringUtils.hasText(convPermitId) && StringUtils.hasText(conversationId)) {
                conversationSemaphore(conversationId)
                        .updateLeaseTime(convPermitId, leaseMillis(), TimeUnit.MILLISECONDS);
            }
        } catch (Exception e) {
            log.debug("permit lease renew failed conv={}: {}", conversationId, e.getMessage());
        }
    }

    public boolean conversationBusy(String conversationId) {
        try {
            return conversationSemaphore(conversationId).availablePermits() <= 0;
        } catch (Exception e) {
            return false;
        }
    }

    public int availableGlobalPermits() {
        try {
            return globalSemaphore().availablePermits();
        } catch (Exception e) {
            return 0;
        }
    }

    public int globalExecutionCapacity() {
        try {
            return globalSemaphore().getPermits();
        } catch (Exception e) {
            return Math.max(1, properties.getMaxGlobalConcurrent());
        }
    }

    private long leaseMillis() {
        return TimeUnit.MINUTES.toMillis(Math.max(5, properties.getPermitLeaseMinutes()));
    }
}
