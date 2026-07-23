package com.resumai.agent.api.hr.dto;

/** HR 下一步行动提示。 */
public record HrNextAction(
        String kind,
        String title,
        String href,
        long count
) {
}
