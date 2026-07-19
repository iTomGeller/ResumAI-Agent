package com.resumai.agent.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Retrieves source-bound public evidence only for URLs explicitly declared in the resume.
 *
 * <p>GitHub facts come from the public GitHub API. A missing, failed, or rate-limited response is
 * represented as structured {@code unavailable}; no profile, repository, or activity value is ever
 * synthesized. Blog URLs are reported as declared links only and are not treated as verified facts.</p>
 */
@Service
public class ExternalProfileService {

    private static final Logger log = LoggerFactory.getLogger(ExternalProfileService.class);
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final Pattern GITHUB_URL_PATTERN = Pattern.compile(
            "(?:https?://)?github\\.com/([a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)"
    );
    private static final Pattern BLOG_URL_PATTERN = Pattern.compile(
            "https?://(?:blog|medium|dev\\.to|juejin\\.cn|www\\.cnblogs\\.com|segmentfault\\.com)[^\\s,;）)\"']*"
    );
    private static final List<String> RESERVED_GITHUB_PATHS = List.of(
            "topics", "explore", "settings", "notifications", "marketplace", "features", "login", "signup"
    );

    private final HttpClient httpClient;

    public ExternalProfileService() {
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .followRedirects(HttpClient.Redirect.NORMAL)
                .build();
    }

    /**
     * Returns a JSON evidence envelope. Candidate facts are present only after a successful real API
     * response and always include their source URLs and retrieval timestamp.
     */
    public String enrich(String resumeText) {
        List<String> githubUsernames = extractGitHubUsernames(resumeText);
        List<String> blogUrls = extractBlogUrls(resumeText);
        List<Map<String, Object>> githubEvidence = githubUsernames.stream()
                .map(this::fetchGitHubEvidence)
                .toList();
        List<Map<String, Object>> declaredLinks = blogUrls.stream()
                .map(this::declaredLinkEvidence)
                .toList();

        boolean hasAvailableGithub = githubEvidence.stream()
                .anyMatch(item -> "available".equals(item.get("status")));
        String status;
        String reason;
        if (hasAvailableGithub) {
            status = "available";
            reason = "source_backed_github_evidence_retrieved";
        } else if (!declaredLinks.isEmpty()) {
            status = "declared-only";
            reason = "candidate_declared_links_not_fetched_or_verified";
        } else if (!githubEvidence.isEmpty()) {
            status = "unavailable";
            reason = "github_evidence_unavailable";
        } else {
            status = "unavailable";
            reason = "no_candidate_declared_external_url";
        }

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("status", status);
        result.put("reason", reason);
        result.put("subjectBinding", "candidate-declared-url-unverified-ownership");
        result.put("github", githubEvidence);
        result.put("declaredLinks", declaredLinks);
        result.put("syntheticFallback", false);
        result.put("limitations", List.of(
                "A declared URL does not prove account ownership.",
                "Public activity is corroborating evidence, not a substitute for resume or interview evidence."
        ));
        return writeJson(result);
    }

    /** Returns a concise trace label without performing network access. */
    public String getSummary(String resumeText) {
        List<String> githubUsernames = extractGitHubUsernames(resumeText);
        List<String> blogUrls = extractBlogUrls(resumeText);
        if (githubUsernames.isEmpty() && blogUrls.isEmpty()) {
            return "简历中未发现候选人声明的 GitHub 或博客链接";
        }
        StringBuilder summary = new StringBuilder();
        if (!githubUsernames.isEmpty()) {
            summary.append("候选人声明 GitHub: ").append(String.join(", ", githubUsernames));
        }
        if (!blogUrls.isEmpty()) {
            if (!summary.isEmpty()) summary.append("；");
            summary.append("候选人声明博客链接: ").append(blogUrls.size()).append(" 个");
        }
        return summary.toString();
    }

    private Map<String, Object> fetchGitHubEvidence(String username) {
        String profileUrl = "https://github.com/" + username;
        String userApiUrl = "https://api.github.com/users/" + username;
        String reposApiUrl = userApiUrl + "/repos?sort=updated&per_page=5";
        Instant retrievedAt = Instant.now();

        Map<String, Object> evidence = baseEvidence("github", profileUrl, retrievedAt);
        HttpResult userResponse = httpGet(userApiUrl, "application/vnd.github+json");
        if (!userResponse.success()) {
            evidence.put("status", "unavailable");
            evidence.put("reason", httpReason(userResponse));
            evidence.put("candidateFact", false);
            evidence.put("apiSourceUrls", List.of(userApiUrl));
            if (userResponse.errorType() != null) {
                evidence.put("errorType", userResponse.errorType());
            }
            return evidence;
        }

        try {
            JsonNode user = JSON.readTree(userResponse.body());
            if (!user.isObject() || !user.hasNonNull("login")) {
                evidence.put("status", "unavailable");
                evidence.put("reason", "invalid_github_api_response");
                evidence.put("candidateFact", false);
                evidence.put("apiSourceUrls", List.of(userApiUrl));
                return evidence;
            }

            Map<String, Object> facts = new LinkedHashMap<>();
            putText(facts, "login", user, "login");
            putText(facts, "name", user, "name");
            putText(facts, "bio", user, "bio");
            putText(facts, "company", user, "company");
            putNumber(facts, "publicRepos", user, "public_repos");
            putNumber(facts, "followers", user, "followers");
            putText(facts, "createdAt", user, "created_at");
            putText(facts, "updatedAt", user, "updated_at");

            HttpResult reposResponse = httpGet(reposApiUrl, "application/vnd.github+json");
            List<Map<String, Object>> repositories = reposResponse.success()
                    ? parseRepositories(reposResponse.body())
                    : List.of();
            facts.put("repositories", repositories);

            evidence.put("status", "available");
            evidence.put("reason", "github_public_api_success");
            evidence.put("candidateFact", true);
            evidence.put("ownershipVerified", false);
            evidence.put("apiSourceUrls", List.of(userApiUrl, reposApiUrl));
            evidence.put("repositoryEvidenceStatus", reposResponse.success()
                    ? "available"
                    : "unavailable: " + httpReason(reposResponse));
            evidence.put("facts", facts);
            return evidence;
        } catch (Exception e) {
            log.warn("GitHub evidence parse failed for candidate-declared handle {}: {}", username, e.getMessage());
            evidence.put("status", "unavailable");
            evidence.put("reason", "invalid_github_api_response");
            evidence.put("candidateFact", false);
            evidence.put("apiSourceUrls", List.of(userApiUrl));
            return evidence;
        }
    }

    private List<Map<String, Object>> parseRepositories(String body) throws Exception {
        JsonNode root = JSON.readTree(body);
        if (!root.isArray()) {
            return List.of();
        }
        List<Map<String, Object>> repositories = new ArrayList<>();
        for (JsonNode repo : root) {
            if (repositories.size() >= 5) break;
            Map<String, Object> item = new LinkedHashMap<>();
            putText(item, "fullName", repo, "full_name");
            putText(item, "sourceUrl", repo, "html_url");
            putText(item, "description", repo, "description");
            putText(item, "language", repo, "language");
            putNumber(item, "stars", repo, "stargazers_count");
            putNumber(item, "forks", repo, "forks_count");
            putText(item, "updatedAt", repo, "updated_at");
            putText(item, "pushedAt", repo, "pushed_at");
            if (!item.isEmpty()) repositories.add(item);
        }
        return repositories;
    }

    private Map<String, Object> declaredLinkEvidence(String url) {
        Map<String, Object> evidence = baseEvidence("declared-web-link", url, Instant.now());
        evidence.put("status", "declared-only");
        evidence.put("reason", "not_fetched_or_verified");
        evidence.put("candidateFact", false);
        return evidence;
    }

    private Map<String, Object> baseEvidence(String provider, String sourceUrl, Instant retrievedAt) {
        Map<String, Object> evidence = new LinkedHashMap<>();
        evidence.put("provider", provider);
        evidence.put("sourceUrl", sourceUrl);
        evidence.put("retrievedAt", retrievedAt.toString());
        evidence.put("subjectBinding", "candidate-declared-url-unverified-ownership");
        evidence.put("syntheticFallback", false);
        return evidence;
    }

    private HttpResult httpGet(String url, String accept) {
        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .header("Accept", accept)
                    .header("User-Agent", "ResumAI-Agent/1.0")
                    .header("X-GitHub-Api-Version", "2022-11-28")
                    .timeout(Duration.ofSeconds(6))
                    .GET();
            String token = githubToken();
            if (StringUtils.hasText(token)) {
                builder.header("Authorization", "Bearer " + token);
            }
            HttpResponse<String> response = httpClient.send(
                    builder.build(), HttpResponse.BodyHandlers.ofString());
            return new HttpResult(response.statusCode(), response.body(), null);
        } catch (Exception e) {
            log.debug("GitHub API request unavailable for {}: {}", url, e.getMessage());
            return new HttpResult(0, "", e.getClass().getSimpleName());
        }
    }

    private String githubToken() {
        String token = System.getenv("GITHUB_TOKEN");
        if (!StringUtils.hasText(token)) {
            token = System.getenv("GITHUB_PERSONAL_ACCESS_TOKEN");
        }
        return token;
    }

    private String httpReason(HttpResult result) {
        if (result.statusCode() == 0) return "network_unavailable";
        if (result.statusCode() == 403 || result.statusCode() == 429) return "rate_limited_or_forbidden";
        if (result.statusCode() == 404) return "profile_not_found";
        return "github_http_" + result.statusCode();
    }

    private List<String> extractGitHubUsernames(String text) {
        if (!StringUtils.hasText(text)) return List.of();
        List<String> usernames = new ArrayList<>();
        Matcher matcher = GITHUB_URL_PATTERN.matcher(text);
        while (matcher.find()) {
            String username = matcher.group(1);
            boolean reserved = RESERVED_GITHUB_PATHS.stream().anyMatch(username::equalsIgnoreCase);
            if (!reserved && !usernames.contains(username)) {
                usernames.add(username);
            }
        }
        return usernames;
    }

    private List<String> extractBlogUrls(String text) {
        if (!StringUtils.hasText(text)) return List.of();
        List<String> urls = new ArrayList<>();
        Matcher matcher = BLOG_URL_PATTERN.matcher(text);
        while (matcher.find()) {
            String url = matcher.group();
            if (!urls.contains(url)) urls.add(url);
        }
        return urls;
    }

    private static void putText(Map<String, Object> target, String targetKey, JsonNode source, String sourceKey) {
        JsonNode value = source.get(sourceKey);
        if (value != null && value.isTextual() && !value.asText().isBlank()) {
            target.put(targetKey, value.asText());
        }
    }

    private static void putNumber(Map<String, Object> target, String targetKey, JsonNode source, String sourceKey) {
        JsonNode value = source.get(sourceKey);
        if (value != null && value.isNumber()) {
            target.put(targetKey, value.numberValue());
        }
    }

    private static String writeJson(Map<String, Object> value) {
        try {
            return JSON.writeValueAsString(value);
        } catch (Exception e) {
            return "{\"status\":\"unavailable\",\"reason\":\"serialization_failed\",\"syntheticFallback\":false}";
        }
    }

    private record HttpResult(int statusCode, String body, String errorType) {
        private boolean success() {
            return statusCode == 200;
        }
    }
}
