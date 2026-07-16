package com.resumai.agent.api.dto;

public record InternalSkillExecuteRequest(
        String skillName,
        String task
) {}
