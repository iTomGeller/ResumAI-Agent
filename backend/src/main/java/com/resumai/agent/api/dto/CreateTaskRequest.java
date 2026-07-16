package com.resumai.agent.api.dto;

import com.resumai.agent.rag.RagOptions;
import jakarta.validation.constraints.NotBlank;

/**
 * 创建简历评估任务请求。
 *
 * <p>该请求用于工作台直接创建评估任务。支持文件上传、
 * 对象存储地址、岗位 JD 和企业规则 ID 的组合输入。</p>
 */
public record CreateTaskRequest(
        @NotBlank(message = "fileName 不能为空")
        String fileName,
        @NotBlank(message = "jobCategory 不能为空")
        String jobCategory,
        @NotBlank(message = "executionMode 不能为空")
        String executionMode,
        String jobDescription,
        String resumeText,
        RagOptions ragOptions
) {
    public CreateTaskRequest(String fileName, String jobCategory, String executionMode,
                             String jobDescription, String resumeText) {
        this(fileName, jobCategory, executionMode, jobDescription, resumeText, null);
    }
}
