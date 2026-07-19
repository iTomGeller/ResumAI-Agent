package com.resumai.agent.service;

import com.resumai.agent.config.WorkflowProperties;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Shared-token authorization for the Docker-internal control plane
 * (/api/internal/*). Run events and results are ingested by
 * {@link com.resumai.agent.api.AgentRunInternalController}; the legacy
 * /workflow/events and /workflow/result callbacks were removed together
 * with the legacy graph runtime.
 */
@Service
public class InternalWorkflowService {

    private final WorkflowProperties workflowProperties;

    public InternalWorkflowService(WorkflowProperties workflowProperties) {
        this.workflowProperties = workflowProperties;
    }

    public boolean authorize(String token) {
        return StringUtils.hasText(workflowProperties.getInternalToken())
                && workflowProperties.getInternalToken().equals(token);
    }
}
