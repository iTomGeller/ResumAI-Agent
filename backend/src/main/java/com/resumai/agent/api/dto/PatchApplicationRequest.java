package com.resumai.agent.api.dto;

/** PATCH 投递申请：更新阶段 / 负责人。 */
public record PatchApplicationRequest(
        String stage,
        String ownerHrId
) {
}
