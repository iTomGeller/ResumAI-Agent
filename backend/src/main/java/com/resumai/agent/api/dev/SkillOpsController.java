package com.resumai.agent.api.dev;

import com.resumai.agent.api.dto.ops.OpsDebugDtos.SkillOpsResponse;
import com.resumai.agent.service.ops.OpsDebugService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/dev/skills")
public class SkillOpsController {

    private final OpsDebugService opsDebugService;

    public SkillOpsController(OpsDebugService opsDebugService) {
        this.opsDebugService = opsDebugService;
    }

    @GetMapping
    public SkillOpsResponse skills(
            @RequestParam(defaultValue = "false") boolean includeDeprecated,
            @RequestParam(defaultValue = "60") int recentLimit) {
        return opsDebugService.skills(includeDeprecated, recentLimit);
    }
}
