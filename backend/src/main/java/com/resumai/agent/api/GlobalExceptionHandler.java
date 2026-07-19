package com.resumai.agent.api;

import com.resumai.agent.api.dto.JdConflictResponse;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class GlobalExceptionHandler {

    @ExceptionHandler(JdVersionConflictException.class)
    public ResponseEntity<JdConflictResponse> handleJdConflict(JdVersionConflictException ex) {
        return ResponseEntity.status(HttpStatus.CONFLICT).body(JdConflictResponse.of(ex.getCurrent()));
    }

    @ExceptionHandler(ApiNotFoundException.class)
    public ResponseEntity<java.util.Map<String, String>> handleNotFound(ApiNotFoundException ex) {
        return ResponseEntity.status(HttpStatus.NOT_FOUND)
                .body(java.util.Map.of("code", "NOT_FOUND", "message", ex.getMessage()));
    }

    @ExceptionHandler(ApiConflictException.class)
    public ResponseEntity<java.util.Map<String, String>> handleConflict(ApiConflictException ex) {
        return ResponseEntity.status(HttpStatus.CONFLICT)
                .body(java.util.Map.of("code", "CONFLICT", "message", ex.getMessage()));
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<java.util.Map<String, String>> handleBadRequest(IllegalArgumentException ex) {
        return ResponseEntity.status(HttpStatus.BAD_REQUEST)
                .body(java.util.Map.of("code", "BAD_REQUEST", "message", ex.getMessage()));
    }
}
