package com.resumai.agent.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.resumai.agent.api.dto.PageResult;
import com.resumai.agent.api.dto.TaskListItemResponse;
import com.resumai.agent.dao.ResumeTaskMapper;
import com.resumai.agent.domain.entity.ResumeTask;
import java.util.ArrayList;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * MySQL 任务列表查询服务 — 候选人列表的权威查询入口。
 */
@Service
public class TaskQueryService {

    private final ResumeTaskMapper resumeTaskMapper;

    public TaskQueryService(ResumeTaskMapper resumeTaskMapper) {
        this.resumeTaskMapper = resumeTaskMapper;
    }

    public PageResult<TaskListItemResponse> queryTasks(
            String keyword,
            String status,
            String recommendation,
            String jobCategory,
            Integer scoreMin,
            Integer scoreMax,
            String sortBy,
            String sortOrder,
            int page,
            int pageSize) {
        int safePage = Math.max(page, 1);
        int safeSize = Math.min(Math.max(pageSize, 1), 100);
        QueryWrapper<ResumeTask> wrapper = new QueryWrapper<>();

        if (StringUtils.hasText(keyword)) {
            String q = keyword.trim();
            wrapper.and(w -> w.like("file_name", q)
                    .or().like("candidate_name", q)
                    .or().like("job_category", q)
                    .or().like("matched_jd_title", q)
                    .or().like("trace_id", q));
        }
        if (StringUtils.hasText(status) && !"ALL".equalsIgnoreCase(status)) {
            wrapper.eq("status", status.trim().toUpperCase());
        }
        if (StringUtils.hasText(recommendation) && !"ALL".equalsIgnoreCase(recommendation)) {
            if ("RECOMMEND".equalsIgnoreCase(recommendation)) {
                wrapper.like("recommendation", "RECOMMEND");
            } else if ("REVIEW".equalsIgnoreCase(recommendation)) {
                wrapper.eq("status", "SUCCESS")
                        .and(w -> w.isNull("recommendation")
                                .or().notLike("recommendation", "RECOMMEND"));
            } else {
                wrapper.eq("recommendation", recommendation.trim());
            }
        }
        if (StringUtils.hasText(jobCategory) && !"ALL".equalsIgnoreCase(jobCategory)) {
            wrapper.eq("job_category", jobCategory.trim().toUpperCase());
        }
        if (scoreMin != null) {
            wrapper.ge("overall_score", scoreMin);
        }
        if (scoreMax != null) {
            wrapper.le("overall_score", scoreMax);
        }

        applySort(wrapper, sortBy, sortOrder);

        Page<ResumeTask> mpPage = resumeTaskMapper.selectPage(new Page<>(safePage, safeSize), wrapper);
        List<TaskListItemResponse> items = new ArrayList<>(mpPage.getRecords().size());
        for (ResumeTask row : mpPage.getRecords()) {
            items.add(toListItem(row));
        }
        return PageResult.of(items, mpPage.getTotal(), safePage, safeSize);
    }

    private void applySort(QueryWrapper<ResumeTask> wrapper, String sortBy, String sortOrder) {
        boolean asc = "asc".equalsIgnoreCase(sortOrder);
        String column = switch (sortBy == null ? "" : sortBy) {
            case "score_desc", "score" -> "overall_score";
            case "score_asc" -> "overall_score";
            case "duration_desc", "duration" -> "duration_ms";
            case "duration_asc" -> "duration_ms";
            case "create_time" -> "create_time";
            default -> "create_time";
        };
        if ("score_asc".equals(sortBy) || "duration_asc".equals(sortBy) || asc) {
            wrapper.orderByAsc(column).orderByDesc("id");
        } else {
            wrapper.orderByDesc(column).orderByDesc("id");
        }
    }

    public TaskListItemResponse toListItem(ResumeTask row) {
        return new TaskListItemResponse(
                row.getId(),
                row.getTraceId(),
                StringUtils.hasText(row.getFileName()) ? row.getFileName() : row.getCandidateName(),
                row.getJobCategory(),
                row.getExecutionMode(),
                row.getStatus(),
                row.getOverallScore(),
                row.getRecommendation(),
                row.getSummary(),
                row.getDurationMs(),
                row.getTokenCost(),
                row.getMatchedJdTitle(),
                row.getJdMatchScore(),
                row.getCreateTime(),
                row.getUpdateTime()
        );
    }
}
