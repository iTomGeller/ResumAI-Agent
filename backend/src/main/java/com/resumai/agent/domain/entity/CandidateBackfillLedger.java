package com.resumai.agent.domain.entity;

import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.LocalDateTime;
import lombok.Data;

/** 候选人历史回填幂等账本：每个 task 最多一行。 */
@Data
@TableName("candidate_backfill_ledger")
public class CandidateBackfillLedger {

    @TableId("task_id")
    private Long taskId;

    @TableField("candidate_id")
    private Long candidateId;

    @TableField("application_id")
    private Long applicationId;

    @TableField("identity_key")
    private String identityKey;

    @TableField("action")
    private String action;

    @TableField("error")
    private String error;

    @TableField("migrated_at")
    private LocalDateTime migratedAt;

    public static CandidateBackfillLedger linked(Long taskId,
                                                 Long candidateId,
                                                 Long applicationId,
                                                 String identityKey) {
        CandidateBackfillLedger row = new CandidateBackfillLedger();
        row.setTaskId(taskId);
        row.setCandidateId(candidateId);
        row.setApplicationId(applicationId);
        row.setIdentityKey(identityKey);
        row.setAction("LINKED");
        row.setMigratedAt(LocalDateTime.now());
        return row;
    }

    public static CandidateBackfillLedger skipped(Long taskId, String reason) {
        CandidateBackfillLedger row = new CandidateBackfillLedger();
        row.setTaskId(taskId);
        row.setAction("SKIPPED");
        row.setError(reason == null ? null : (reason.length() > 1000 ? reason.substring(0, 1000) : reason));
        row.setMigratedAt(LocalDateTime.now());
        return row;
    }

    public static CandidateBackfillLedger failed(Long taskId, String error) {
        CandidateBackfillLedger row = new CandidateBackfillLedger();
        row.setTaskId(taskId);
        row.setAction("FAILED");
        row.setError(error == null ? null : (error.length() > 1000 ? error.substring(0, 1000) : error));
        row.setMigratedAt(LocalDateTime.now());
        return row;
    }
}
