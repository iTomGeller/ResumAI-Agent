package com.resumai.agent.config;

import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.util.StringUtils;

import java.nio.charset.StandardCharsets;
import java.util.Base64;

import static io.opentelemetry.api.common.AttributeKey.stringKey;

@Configuration
public class LangfuseOtelConfig {

    private static final Logger log = LoggerFactory.getLogger(LangfuseOtelConfig.class);

    @Bean
    public OpenTelemetry openTelemetry(
            @Value("${langfuse.otel-endpoint:}") String endpoint,
            @Value("${langfuse.public-key:}") String publicKey,
            @Value("${langfuse.secret-key:}") String secretKey) {

        if (!StringUtils.hasText(endpoint)) {
            log.info("[langfuse] LANGFUSE_OTEL_ENDPOINT not set, OTel tracing disabled");
            return OpenTelemetry.noop();
        }

        String auth = Base64.getEncoder().encodeToString(
                (publicKey + ":" + secretKey).getBytes(StandardCharsets.UTF_8));

        OtlpHttpSpanExporter exporter = OtlpHttpSpanExporter.builder()
                .setEndpoint(endpoint)
                .addHeader("Authorization", "Basic " + auth)
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

        log.info("[langfuse] OTel tracing enabled → {}", endpoint);
        return sdk;
    }
}
