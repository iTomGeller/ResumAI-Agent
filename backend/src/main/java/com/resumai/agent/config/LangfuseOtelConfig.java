package com.resumai.agent.config;

import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;
import jakarta.annotation.PostConstruct;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import static io.opentelemetry.api.common.AttributeKey.stringKey;

@Configuration
public class LangfuseOtelConfig {

    private static final Logger log = LoggerFactory.getLogger(LangfuseOtelConfig.class);

    private final LangfuseHealthService langfuseHealth;

    public LangfuseOtelConfig(LangfuseHealthService langfuseHealth) {
        this.langfuseHealth = langfuseHealth;
    }

    @PostConstruct
    void probeOnStartup() {
        if (langfuseHealth.isExporterEnabled()) {
            langfuseHealth.refreshProbe();
        } else {
            log.info("[langfuse] exporter disabled — {}", langfuseHealth.disableReason());
        }
    }

    @Bean
    public OpenTelemetry openTelemetry() {
        if (!langfuseHealth.isExporterEnabled()) {
            log.info("[langfuse] OTel tracing disabled — {}", langfuseHealth.disableReason());
            return OpenTelemetry.noop();
        }

        String endpoint = langfuseHealth.getOtelEndpoint();
        String auth = Base64.getEncoder().encodeToString(
                (langfuseHealth.getPublicKey() + ":" + langfuseHealth.getSecretKey())
                        .getBytes(StandardCharsets.UTF_8));

        OtlpHttpSpanExporter exporter = OtlpHttpSpanExporter.builder()
                .setEndpoint(endpoint)
                .addHeader("Authorization", "Basic " + auth)
                .addHeader("x-langfuse-ingestion-version", "4")
                .build();

        Resource resource = Resource.getDefault().merge(
                Resource.create(Attributes.of(stringKey("service.name"), "resumai-agent")));

        SdkTracerProvider tracerProvider = SdkTracerProvider.builder()
                .addSpanProcessor(BatchSpanProcessor.builder(exporter).build())
                .setResource(resource)
                .build();

        OpenTelemetrySdk sdk = OpenTelemetrySdk.builder()
                .setTracerProvider(tracerProvider)
                .buildAndRegisterGlobal();

        log.info("[langfuse] OTel tracing enabled → {} (ingestion-version=4)",
                LangfuseHealthService.redactEndpoint(endpoint));
        return sdk;
    }
}
