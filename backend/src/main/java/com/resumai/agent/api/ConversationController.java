package com.resumai.agent.api;

import com.resumai.agent.api.dto.ConversationSnapshotResponse;
import com.resumai.agent.api.dto.ConversationTurnRequest;
import com.resumai.agent.api.dto.ConversationTurnResponse;
import com.resumai.agent.api.dto.TaskControlRequest;
import com.resumai.agent.api.dto.TaskControlResponse;
import com.resumai.agent.service.ConversationService;
import com.resumai.agent.service.TaskControlService;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api")
public class ConversationController {

    private final ConversationService conversationService;
    private final TaskControlService taskControlService;

    public ConversationController(ConversationService conversationService,
                                  TaskControlService taskControlService) {
        this.conversationService = conversationService;
        this.taskControlService = taskControlService;
    }

    @GetMapping("/conversations/{conversationId}")
    public ConversationSnapshotResponse getConversation(@PathVariable String conversationId) {
        return conversationService.getSnapshot(conversationId);
    }

    @PostMapping("/conversations/{conversationId}/messages")
    public ConversationTurnResponse sendMessage(
            @PathVariable String conversationId,
            @Valid @RequestBody ConversationTurnRequest request) {
        return conversationService.sendTurn(conversationId, request);
    }

    @PostMapping("/tasks/{traceId}/control")
    public TaskControlResponse controlTask(
            @PathVariable String traceId,
            @Valid @RequestBody TaskControlRequest request) {
        return taskControlService.control(traceId, request.action());
    }
}
