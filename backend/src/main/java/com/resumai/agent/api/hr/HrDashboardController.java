package com.resumai.agent.api.hr;

import com.resumai.agent.api.hr.dto.HrDashboardResponse;
import com.resumai.agent.service.hr.HrDashboardService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/hr")
public class HrDashboardController {

    private final HrDashboardService hrDashboardService;

    public HrDashboardController(HrDashboardService hrDashboardService) {
        this.hrDashboardService = hrDashboardService;
    }

    @GetMapping("/dashboard")
    public HrDashboardResponse dashboard() {
        return hrDashboardService.dashboard();
    }
}
