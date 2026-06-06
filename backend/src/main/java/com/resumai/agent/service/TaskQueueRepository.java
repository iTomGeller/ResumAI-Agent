package com.resumai.agent.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.UpdateWrapper;
import com.resumai.agent.dao.ResumeTaskMapper;
import com.resumai.agent.domain.entity.ResumeTask;
import com.resumai.agent.domain.enums.QueueStatus;
import java.time.LocalDateTime;
import java.util.List;
import org.springframework.stereotype.Service;

@Service
public class TaskQueueRepository {

    private final ResumeTaskMapper resumeTaskMapper;

    public TaskQueueRepository(ResumeTaskMapper resumeTaskMapper) {
        this.resumeTaskMapper = resumeTaskMapper;
    }

    public int claimTask(String traceId, String workerId) {
        UpdateWrapper<ResumeTask> wrapper = new UpdateWrapper<>();
        wrapper.eq("trace_id", traceId)
                .in("queue_status", QueueStatus.QUEUED.name(), QueueStatus.RETRYING.name())
                .set("queue_status", QueueStatus.RUNNING.name())
                .set("status", "RUNNING")
                .set("worker_id", workerId)
                .set("started_at", LocalDateTime.now())
                .set("update_time", LocalDateTime.now());
        return resumeTaskMapper.update(null, wrapper);
    }

    public void markQueueSuccess(String traceId) {
        UpdateWrapper<ResumeTask> wrapper = new UpdateWrapper<>();
        wrapper.eq("trace_id", traceId)
                .set("queue_status", QueueStatus.SUCCESS.name())
                .set("finished_at", LocalDateTime.now())
                .set("update_time", LocalDateTime.now());
        resumeTaskMapper.update(null, wrapper);
    }

    public void markQueueFailed(String traceId, String failReason) {
        UpdateWrapper<ResumeTask> wrapper = new UpdateWrapper<>();
        wrapper.eq("trace_id", traceId)
                .set("queue_status", QueueStatus.FAILED.name())
                .set("status", "FAILED")
                .set("fail_reason", failReason)
                .set("finished_at", LocalDateTime.now())
                .set("update_time", LocalDateTime.now());
        resumeTaskMapper.update(null, wrapper);
    }

    public void markRetrying(String traceId, int attemptCount, LocalDateTime nextRetryAt) {
        UpdateWrapper<ResumeTask> wrapper = new UpdateWrapper<>();
        wrapper.eq("trace_id", traceId)
                .set("queue_status", QueueStatus.RETRYING.name())
                .set("status", "QUEUED")
                .set("attempt_count", attemptCount)
                .set("next_retry_at", nextRetryAt)
                .set("worker_id", null)
                .set("update_time", LocalDateTime.now());
        resumeTaskMapper.update(null, wrapper);
    }

    public long countByQueueStatus(String queueStatus) {
        QueryWrapper<ResumeTask> wrapper = new QueryWrapper<>();
        wrapper.eq("queue_status", queueStatus);
        Long count = resumeTaskMapper.selectCount(wrapper);
        return count != null ? count : 0L;
    }

    public long countRunningFresh(LocalDateTime cutoff) {
        QueryWrapper<ResumeTask> wrapper = new QueryWrapper<>();
        wrapper.eq("queue_status", QueueStatus.RUNNING.name())
                .ge("started_at", cutoff);
        Long count = resumeTaskMapper.selectCount(wrapper);
        return count != null ? count : 0L;
    }

    public long countStuckRunning(LocalDateTime cutoff) {
        QueryWrapper<ResumeTask> wrapper = new QueryWrapper<>();
        wrapper.eq("queue_status", QueueStatus.RUNNING.name())
                .and(w -> w.isNull("started_at").or().lt("started_at", cutoff));
        Long count = resumeTaskMapper.selectCount(wrapper);
        return count != null ? count : 0L;
    }

    public LocalDateTime oldestQueuedAt() {
        QueryWrapper<ResumeTask> wrapper = new QueryWrapper<>();
        wrapper.in("queue_status", QueueStatus.QUEUED.name(), QueueStatus.RETRYING.name())
                .orderByAsc("queued_at")
                .last("limit 1");
        ResumeTask row = resumeTaskMapper.selectOne(wrapper);
        return row != null ? row.getQueuedAt() : null;
    }

    public List<ResumeTask> listByQueueStatus(String queueStatus, int limit) {
        QueryWrapper<ResumeTask> wrapper = new QueryWrapper<>();
        wrapper.eq("queue_status", queueStatus)
                .orderByDesc("update_time")
                .last("limit " + Math.max(limit, 1));
        return resumeTaskMapper.selectList(wrapper);
    }

    public int recoverStuckRunning(LocalDateTime cutoff) {
        UpdateWrapper<ResumeTask> wrapper = new UpdateWrapper<>();
        wrapper.eq("queue_status", QueueStatus.RUNNING.name())
                .and(w -> w.isNull("started_at").or().lt("started_at", cutoff))
                .set("queue_status", QueueStatus.RETRYING.name())
                .set("status", "QUEUED")
                .set("worker_id", null)
                .set("next_retry_at", LocalDateTime.now())
                .set("update_time", LocalDateTime.now());
        return resumeTaskMapper.update(null, wrapper);
    }

    public List<ResumeTask> listStuckForRequeue(LocalDateTime cutoff, int limit) {
        QueryWrapper<ResumeTask> wrapper = new QueryWrapper<>();
        wrapper.eq("queue_status", QueueStatus.RETRYING.name())
                .and(w -> w.isNull("next_retry_at").or().le("next_retry_at", LocalDateTime.now()))
                .orderByAsc("priority", "queued_at")
                .last("limit " + Math.max(limit, 1));
        return resumeTaskMapper.selectList(wrapper);
    }
}
