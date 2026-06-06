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
}
