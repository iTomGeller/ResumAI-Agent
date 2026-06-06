package com.resumai.agent.api;

import com.resumai.agent.api.dto.JdDetailResponse;

public class JdVersionConflictException extends RuntimeException {

    private final JdDetailResponse current;

    public JdVersionConflictException(JdDetailResponse current) {
        super("JD version conflict");
        this.current = current;
    }

    public JdDetailResponse getCurrent() {
        return current;
    }
}
