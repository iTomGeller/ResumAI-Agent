package com.resumai.agent.service;



import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;

import com.baomidou.mybatisplus.extension.plugins.pagination.Page;

import com.resumai.agent.api.dto.LlmInvocationResponse;

import com.resumai.agent.api.dto.PageResult;

import com.resumai.agent.dao.LlmInvocationMapper;

import com.resumai.agent.domain.entity.LlmInvocation;

import java.time.LocalDateTime;

import java.util.ArrayList;

import java.util.Comparator;

import java.util.LinkedHashMap;

import java.util.List;

import java.util.Map;

import java.util.UUID;

import java.util.concurrent.ConcurrentHashMap;

import java.util.regex.Pattern;

import org.slf4j.Logger;

import org.slf4j.LoggerFactory;

import org.springframework.dao.DataAccessException;

import org.springframework.stereotype.Service;

import org.springframework.util.StringUtils;



@Service

public class LlmInvocationService {



    private static final Logger log = LoggerFactory.getLogger(LlmInvocationService.class);

    private static final int PREVIEW_LENGTH = 500;

    private static final int CACHE_MAX_SIZE = 500;

    private static final Pattern PHONE = Pattern.compile("1[3-9]\\d{9}");

    private static final Pattern EMAIL = Pattern.compile("[\\w.+-]+@[\\w.-]+\\.[A-Za-z]{2,}");

    private static final Pattern API_KEY = Pattern.compile("(sk-[A-Za-z0-9_-]{8,}|api[_-]?key\\s*[:=]\\s*\\S+)", Pattern.CASE_INSENSITIVE);



    private final LlmInvocationMapper llmInvocationMapper;

    private final Map<String, LlmInvocation> cache = new ConcurrentHashMap<>();



    public LlmInvocationService(LlmInvocationMapper llmInvocationMapper) {

        this.llmInvocationMapper = llmInvocationMapper;

    }



    public LlmInvocation saveInvocation(String traceId,

                                        String spanId,

                                        String modelName,

                                        String agentRole,

                                        String purpose,

                                        long durationMs,

                                        String prompt,

                                        String response,

                                        Integer inputTokens,

                                        Integer outputTokens,

                                        String finishReason,

                                        String errorCode,

                                        String errorBody) {

        String id = "llm-" + UUID.randomUUID();

        String sanitizedPrompt = sanitize(prompt);

        String sanitizedResponse = sanitize(response);

        LocalDateTime now = LocalDateTime.now();

        LlmInvocation entity = new LlmInvocation();

        entity.setId(id);

        entity.setTraceId(traceId);

        entity.setSpanId(spanId);

        entity.setModelName(modelName);

        entity.setAgentRole(agentRole);

        entity.setPurpose(purpose);

        entity.setRequestStartedAt(now.minusNanos(durationMs * 1_000_000L));

        entity.setDurationMs(durationMs);

        entity.setInputTokens(inputTokens);

        entity.setOutputTokens(outputTokens);

        entity.setFinishReason(StringUtils.hasText(finishReason) ? finishReason : (StringUtils.hasText(errorCode) ? "error" : "stop"));

        entity.setTruncated(isTruncated(response, finishReason) ? 1 : 0);

        entity.setPromptChars(sanitizedPrompt == null ? 0 : sanitizedPrompt.length());

        entity.setResponseChars(sanitizedResponse == null ? 0 : sanitizedResponse.length());

        entity.setPromptPreview(preview(sanitizedPrompt));

        entity.setResponsePreview(preview(sanitizedResponse));



        entity.setPromptFull(sanitizedPrompt);

        entity.setResponseFull(sanitizedResponse);



        entity.setErrorCode(errorCode);

        entity.setErrorBody(errorBody);

        entity.setCreateTime(now);

        entity.setUpdateTime(now);

        entity.setDeleted(0);

        putCache(entity);

        try {

            llmInvocationMapper.insert(entity);

        } catch (DataAccessException e) {

            log.warn("[llm] persist llm_invocation failed (id={}): {}", id, e.getMessage());

        }

        return entity;

    }



    public PageResult<LlmInvocationResponse> queryInvocations(String traceId, String agentRole, int page, int pageSize) {

        int safePage = Math.max(page, 1);

        int safeSize = Math.min(Math.max(pageSize, 1), 100);

        LambdaQueryWrapper<LlmInvocation> wrapper = new LambdaQueryWrapper<>();

        if (StringUtils.hasText(traceId)) {

            wrapper.eq(LlmInvocation::getTraceId, traceId.trim());

        }

        if (StringUtils.hasText(agentRole)) {

            wrapper.eq(LlmInvocation::getAgentRole, agentRole.trim());

        }

        wrapper.orderByDesc(LlmInvocation::getRequestStartedAt);

        Page<LlmInvocation> mpPage = llmInvocationMapper.selectPage(new Page<>(safePage, safeSize), wrapper);

        List<LlmInvocationResponse> items = new ArrayList<>(mpPage.getRecords().size());

        for (LlmInvocation row : mpPage.getRecords()) {

            putCache(row);

            items.add(toResponse(row, false));

        }

        return PageResult.of(items, mpPage.getTotal(), safePage, safeSize);

    }



    public List<LlmInvocationResponse> listByTraceId(String traceId) {

        PageResult<LlmInvocationResponse> page = queryInvocations(traceId, null, 1, 100);

        return page.items();

    }



    public LlmInvocationResponse getInvocation(String id) {

        LlmInvocation entity = cache.get(id);

        if (entity == null) {

            try {

                entity = llmInvocationMapper.selectById(id);

            } catch (DataAccessException e) {

                log.warn("[llm] load llm_invocation failed (id={}): {}", id, e.getMessage());

            }

        }

        if (entity == null) {

            throw new IllegalArgumentException("LLM 调用记录不存在：" + id);

        }

        return toResponse(entity, true);

    }



    public LlmInvocationResponse toResponse(LlmInvocation entity) {

        return toResponse(entity, false);

    }



    public LlmInvocationResponse toResponse(LlmInvocation entity, boolean loadFullText) {

        String promptFull = entity.getPromptFull();

        String responseFull = entity.getResponseFull();

        return new LlmInvocationResponse(

                entity.getId(),

                entity.getTraceId(),

                entity.getSpanId(),

                entity.getModelName(),

                entity.getAgentRole(),

                entity.getPurpose(),

                entity.getRequestStartedAt(),

                entity.getDurationMs(),

                entity.getInputTokens(),

                entity.getOutputTokens(),

                entity.getFinishReason(),

                entity.getTruncated() != null && entity.getTruncated() == 1,

                entity.getPromptChars(),

                entity.getResponseChars(),

                entity.getPromptPreview(),

                entity.getResponsePreview(),

                loadFullText ? promptFull : null,

                loadFullText ? responseFull : null,

                entity.getErrorCode()

        );

    }



    private void putCache(LlmInvocation entity) {

        if (entity == null || !StringUtils.hasText(entity.getId())) {

            return;

        }

        cache.put(entity.getId(), entity);

        trimCache();

    }



    private void trimCache() {

        if (cache.size() <= CACHE_MAX_SIZE) {

            return;

        }

        cache.entrySet().stream()

                .sorted(Map.Entry.comparingByValue(

                        Comparator.comparing(LlmInvocation::getCreateTime, Comparator.nullsLast(Comparator.naturalOrder()))))

                .limit(cache.size() - CACHE_MAX_SIZE)

                .map(Map.Entry::getKey)

                .toList()

                .forEach(cache::remove);

    }



    private String preview(String text) {

        if (!StringUtils.hasText(text)) {

            return "";

        }

        return text.length() <= PREVIEW_LENGTH ? text : text.substring(0, PREVIEW_LENGTH) + "...";

    }



    private boolean isTruncated(String response, String finishReason) {

        return StringUtils.hasText(finishReason) && finishReason.toLowerCase().contains("length");

    }



    String sanitize(String text) {

        if (!StringUtils.hasText(text)) {

            return text;

        }

        String sanitized = PHONE.matcher(text).replaceAll("[手机号已脱敏]");

        sanitized = EMAIL.matcher(sanitized).replaceAll("[邮箱已脱敏]");

        sanitized = API_KEY.matcher(sanitized).replaceAll("[密钥已脱敏]");

        return sanitized;

    }

}

