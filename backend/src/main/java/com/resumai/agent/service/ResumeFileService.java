package com.resumai.agent.service;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;
import org.springframework.web.multipart.MultipartFile;

@Service
public class ResumeFileService {

    private static final Logger log = LoggerFactory.getLogger(ResumeFileService.class);

    private final Path uploadRoot;

    public ResumeFileService(@Value("${resumai.upload-dir:./uploads}") String uploadDir) {
        this.uploadRoot = Paths.get(uploadDir).toAbsolutePath().normalize();
        try {
            Files.createDirectories(uploadRoot);
        } catch (IOException e) {
            throw new IllegalStateException("无法创建简历上传目录: " + uploadRoot, e);
        }
    }

    /**
     * 持久化原始简历文件，按 traceId 命名，防止路径穿越。
     */
    public String save(String traceId, MultipartFile file, String fileType) {
        if (file == null || file.isEmpty() || !StringUtils.hasText(traceId)) {
            return null;
        }
        String safeTraceId = traceId.replaceAll("[^a-zA-Z0-9\\-_]", "");
        String extension = extensionFor(fileType, file.getOriginalFilename());
        Path target = uploadRoot.resolve(safeTraceId + extension).normalize();
        if (!target.startsWith(uploadRoot)) {
            throw new IllegalArgumentException("非法文件路径");
        }
        try {
            Files.copy(file.getInputStream(), target, StandardCopyOption.REPLACE_EXISTING);
            log.info("Saved resume file for trace {} -> {}", traceId, target);
            return target.toString();
        } catch (IOException e) {
            log.warn("Failed to save resume file for {}: {}", traceId, e.getMessage());
            return null;
        }
    }

    public Path resolveForTrace(String traceId) {
        if (!StringUtils.hasText(traceId)) {
            return null;
        }
        String safeTraceId = traceId.replaceAll("[^a-zA-Z0-9\\-_]", "");
        for (String ext : new String[]{".pdf", ".txt", ".md", ".csv"}) {
            Path candidate = uploadRoot.resolve(safeTraceId + ext).normalize();
            if (candidate.startsWith(uploadRoot) && Files.isRegularFile(candidate)) {
                return candidate;
            }
        }
        return null;
    }

    public String detectContentType(Path path) {
        if (path == null) {
            return "application/octet-stream";
        }
        String name = path.getFileName().toString().toLowerCase();
        if (name.endsWith(".pdf")) {
            return "application/pdf";
        }
        if (name.endsWith(".txt") || name.endsWith(".md") || name.endsWith(".csv")) {
            return "text/plain; charset=utf-8";
        }
        return "application/octet-stream";
    }

    private String extensionFor(String fileType, String originalName) {
        if ("pdf".equals(fileType)) {
            return ".pdf";
        }
        if ("txt".equals(fileType)) {
            return ".txt";
        }
        if ("md".equals(fileType)) {
            return ".md";
        }
        if ("csv".equals(fileType)) {
            return ".csv";
        }
        if (StringUtils.hasText(originalName) && originalName.contains(".")) {
            return originalName.substring(originalName.lastIndexOf('.')).toLowerCase();
        }
        return ".bin";
    }
}
