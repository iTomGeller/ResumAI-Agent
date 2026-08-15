package com.resumai.agent.api.dto;

public record InternalJdFocusRequest(
        String jdText,
        String jobTitle,
        String jobCategory
) {}
