package com.resumai.agent.service;

import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.resumai.agent.dao.ConversationTurnMapper;
import com.resumai.agent.domain.entity.ConversationTurn;
import java.time.LocalDateTime;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

/**
 * Lightweight Copilot turns (DIRECT_REPLY / BACKGROUND_QUERY).
 * These rows are intentionally separate from {@code agent_run} and must not
 * enter Policy reward scoring.
 */
@Service
public class ConversationTurnService {

    private static final Logger log = LoggerFactory.getLogger(ConversationTurnService.class);

    private final ConversationTurnMapper turnMapper;
    private final ObjectMapper objectMapper;

    public ConversationTurnService(ConversationTurnMapper turnMapper, ObjectMapper objectMapper) {
        this.turnMapper = turnMapper;
        this.objectMapper = objectMapper;
    }

    public ConversationTurn start(String conversationId,
                                  String clientMessageId,
                                  String disposition,
                                  String intent,
                                  String content) {
        ConversationTurn existing = findByClientMessage(conversationId, clientMessageId);
        if (existing != null) {
            return existing;
        }
        ConversationTurn turn = new ConversationTurn();
        turn.setTurnId("turn-" + UUID.randomUUID());
        turn.setConversationId(conversationId);
        turn.setClientMessageId(clientMessageId);
        turn.setDisposition(disposition);
        turn.setIntent(intent);
        turn.setStatus("PENDING");
        turn.setContent(content == null ? "" : content);
        turn.setCreatedAt(LocalDateTime.now());
        try {
            turnMapper.insert(turn);
        } catch (DuplicateKeyException dup) {
            ConversationTurn winner = findByClientMessage(conversationId, clientMessageId);
            if (winner != null) {
                return winner;
            }
            throw dup;
        }
        return turn;
    }

    public ConversationTurn complete(String turnId,
                                     String answer,
                                     List<Map<String, Object>> citations,
                                     List<Map<String, Object>> actions) {
        ConversationTurn turn = turnMapper.selectById(turnId);
        if (turn == null) {
            throw new IllegalArgumentException("conversation_turn not found: " + turnId);
        }
        turn.setAnswer(answer);
        turn.setCitations(toJson(citations));
        turn.setActions(toJson(actions));
        turn.setStatus("COMPLETED");
        turn.setFinishedAt(LocalDateTime.now());
        turn.setError(null);
        turnMapper.updateById(turn);
        return turn;
    }

    public ConversationTurn fail(String turnId, String error) {
        ConversationTurn turn = turnMapper.selectById(turnId);
        if (turn == null) {
            return null;
        }
        turn.setStatus("FAILED");
        turn.setError(trimError(error));
        turn.setFinishedAt(LocalDateTime.now());
        turnMapper.updateById(turn);
        return turn;
    }

    public long countByConversation(String conversationId) {
        if (!StringUtils.hasText(conversationId)) {
            return 0L;
        }
        Long count = turnMapper.selectCount(new QueryWrapper<ConversationTurn>()
                .eq("conversation_id", conversationId));
        return count == null ? 0L : count;
    }

    public ConversationTurn findByClientMessage(String conversationId, String clientMessageId) {
        if (!StringUtils.hasText(conversationId) || !StringUtils.hasText(clientMessageId)) {
            return null;
        }
        return turnMapper.selectOne(new QueryWrapper<ConversationTurn>()
                .eq("conversation_id", conversationId)
                .eq("client_message_id", clientMessageId)
                .last("limit 1"));
    }

    private String toJson(Object value) {
        if (value == null) {
            return null;
        }
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception e) {
            log.debug("conversation_turn json encode skipped: {}", e.getMessage());
            return null;
        }
    }

    private static String trimError(String error) {
        if (error == null) {
            return null;
        }
        return error.length() > 1000 ? error.substring(0, 1000) : error;
    }
}
