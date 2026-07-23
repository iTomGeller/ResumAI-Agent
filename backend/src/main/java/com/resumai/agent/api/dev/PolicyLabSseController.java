package com.resumai.agent.api.dev;

import com.resumai.agent.api.dto.policylab.PolicyExperimentEventView;
import com.resumai.agent.service.policylab.PolicyLabEventService;
import java.io.IOException;
import java.util.List;
import java.util.concurrent.TimeUnit;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/** Alias matching plan path GET /sse/policy-lab/{id}. */
@RestController
@RequestMapping("/sse/policy-lab")
public class PolicyLabSseController {

    private final PolicyLabEventService eventService;

    public PolicyLabSseController(PolicyLabEventService eventService) {
        this.eventService = eventService;
    }

    @GetMapping(value = "/{id}", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter stream(@PathVariable("id") String id,
                             @RequestParam(defaultValue = "0") int afterSeq) {
        SseEmitter emitter = new SseEmitter(TimeUnit.MINUTES.toMillis(30));
        Thread worker = new Thread(() -> {
            int cursor = afterSeq;
            try {
                for (int i = 0; i < 600; i++) {
                    List<PolicyExperimentEventView> batch = eventService.listAfter(id, cursor, 50);
                    for (PolicyExperimentEventView event : batch) {
                        emitter.send(SseEmitter.event()
                                .name(event.eventType())
                                .id(String.valueOf(event.seq()))
                                .data(event));
                        cursor = event.seq();
                    }
                    Thread.sleep(1000L);
                }
                emitter.complete();
            } catch (IOException | InterruptedException ex) {
                emitter.completeWithError(ex);
                Thread.currentThread().interrupt();
            }
        }, "sse-policy-lab-" + id);
        worker.setDaemon(true);
        worker.start();
        return emitter;
    }
}
