package com.resumai.agent.api.dto;

public record InternalResumeSearchRequest(
        String query,
        Integer topK,
        String resumeText,
        String jdRequirements,
        String strategy
) {}
