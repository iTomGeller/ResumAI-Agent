package com.resumai.agent.api.dto;

import java.time.LocalDateTime;

public record JdConflictResponse(
        String message,
        JdDetailResponse current
) {
    public static JdConflictResponse of(JdDetailResponse current) {
        return new JdConflictResponse("该岗位已被其他 HR 修改，请刷新后重新编辑。", current);
    }
}
