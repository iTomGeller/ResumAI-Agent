package com.resumai.agent.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "resumai.workflow")
public class WorkflowProperties {

    /**
     * Legacy mode switch. Java orchestrator path has been removed; production
     * always runs through the unified Python Agent Runtime. Kept for
     * application.yml binding compatibility — do not reintroduce a java branch.
     */
    private String mode = "python";
    private String baseUrl = "http://ai-resume-workflow:8090";
    private String internalToken = "change-me";

    public String getMode() {
        return mode;
    }

    public void setMode(String mode) {
        this.mode = mode;
    }

    public String getBaseUrl() {
        return baseUrl;
    }

    public void setBaseUrl(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    public String getInternalToken() {
        return internalToken;
    }

    public void setInternalToken(String internalToken) {
        this.internalToken = internalToken;
    }

    public boolean isPythonMode() {
        return "python".equalsIgnoreCase(mode);
    }
}
