package com.resumai.agent.api.dto;

public record InternalJdSearchRequest(
        String resumeText,
        Integer topK
) {}
