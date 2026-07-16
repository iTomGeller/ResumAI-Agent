package com.resumai.agent.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Extracts GitHub/blog URLs from resume text and fetches public profile data
 * to enrich AI evaluation with real external evidence.
 */
@Service
public class ExternalProfileService {

    private static final Logger log = LoggerFactory.getLogger(ExternalProfileService.class);

    private static final Pattern GITHUB_URL_PATTERN = Pattern.compile(
            "(?:https?://)?github\\.com/([a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)"
    );

    private static final Pattern BLOG_URL_PATTERN = Pattern.compile(
            "https?://(?:blog|medium|dev\\.to|juejin\\.cn|www\\.cnblogs\\.com|segmentfault\\.com)[^\\s,;）)\"']*"
    );

    private final HttpClient httpClient;

    public ExternalProfileService() {
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .followRedirects(HttpClient.Redirect.NORMAL)
                .build();
    }

    /**
     * Extract external URLs from resume text and fetch public profiles.
     * Returns a structured summary string for LLM context, or empty string if no URLs found.
     */
    public String enrich(String resumeText) {
        if (!StringUtils.hasText(resumeText)) return "";

        List<String> githubUsernames = extractGitHubUsernames(resumeText);
        List<String> blogUrls = extractBlogUrls(resumeText);

        if (githubUsernames.isEmpty() && blogUrls.isEmpty()) return "";

        StringBuilder sb = new StringBuilder();
        sb.append("\n\n=== 外部作品与公开资料 ===\n");

        for (String username : githubUsernames) {
            String profile = fetchGitHubProfile(username);
            if (StringUtils.hasText(profile)) {
                sb.append("\n【GitHub: ").append(username).append("】\n");
                sb.append(profile).append("\n");
            }
        }

        for (String url : blogUrls) {
            sb.append("\n【博客链接】").append(url).append("\n");
        }

        return sb.toString();
    }

    /**
     * Returns a human-readable summary of what was found, for trace display.
     */
    public String getSummary(String resumeText) {
        List<String> githubUsernames = extractGitHubUsernames(resumeText);
        List<String> blogUrls = extractBlogUrls(resumeText);
        if (githubUsernames.isEmpty() && blogUrls.isEmpty()) return "简历中未发现 GitHub 或博客链接";
        StringBuilder sb = new StringBuilder();
        if (!githubUsernames.isEmpty()) sb.append("GitHub: ").append(String.join(", ", githubUsernames));
        if (!blogUrls.isEmpty()) {
            if (sb.length() > 0) sb.append("；");
            sb.append("博客: ").append(blogUrls.size()).append(" 个链接");
        }
        return sb.toString();
    }

    private List<String> extractGitHubUsernames(String text) {
        List<String> usernames = new ArrayList<>();
        Matcher m = GITHUB_URL_PATTERN.matcher(text);
        while (m.find()) {
            String username = m.group(1);
            if (!username.equalsIgnoreCase("topics") && !username.equalsIgnoreCase("explore")
                    && !username.equalsIgnoreCase("settings") && !username.equalsIgnoreCase("notifications")
                    && !usernames.contains(username)) {
                usernames.add(username);
            }
        }
        return usernames;
    }

    private List<String> extractBlogUrls(String text) {
        List<String> urls = new ArrayList<>();
        Matcher m = BLOG_URL_PATTERN.matcher(text);
        while (m.find()) {
            String url = m.group();
            if (!urls.contains(url)) urls.add(url);
        }
        return urls;
    }

    private String fetchGitHubProfile(String username) {
        try {
            String userJson = httpGet("https://api.github.com/users/" + username);
            if (userJson == null) return null;

            String bio = extractJsonString(userJson, "bio");
            String publicRepos = extractJsonString(userJson, "public_repos");
            String followers = extractJsonString(userJson, "followers");
            String company = extractJsonString(userJson, "company");

            StringBuilder profile = new StringBuilder();
            if (StringUtils.hasText(bio)) profile.append("简介: ").append(bio).append("\n");
            if (StringUtils.hasText(company)) profile.append("公司: ").append(company).append("\n");
            profile.append("公开仓库: ").append(publicRepos != null ? publicRepos : "N/A");
            profile.append(" | 关注者: ").append(followers != null ? followers : "N/A").append("\n");

            String reposJson = httpGet("https://api.github.com/users/" + username + "/repos?sort=stars&per_page=2");
            if (reposJson != null && reposJson.startsWith("[")) {
                List<String> repos = parseTopRepos(reposJson);
                if (!repos.isEmpty()) {
                    profile.append("代表项目（含公开元数据与 README 摘要）:\n");
                    for (String repo : repos) {
                        profile.append("  - ").append(repo).append("\n");
                    }
                } else {
                    profile.append("代表项目: 未解析到可用仓库详情，外部画像不能作为项目深度证据。\n");
                }
            }

            return profile.toString();
        } catch (Exception e) {
            log.warn("Failed to fetch GitHub profile for {}: {}", username, e.getMessage());
            return null;
        }
    }

    private String httpGet(String url) {
        try {
            return httpGet(url, "application/vnd.github+json");
        } catch (Exception e) {
            log.debug("HTTP GET failed for {}: {}", url, e.getMessage());
            return null;
        }
    }

    private String httpGet(String url, String accept) {
        try {
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .header("Accept", accept)
                    .header("User-Agent", "ResumAI-Agent/1.0")
                    .timeout(Duration.ofSeconds(4))
                    .GET()
                    .build();
            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() == 200) return response.body();
            return null;
        } catch (Exception e) {
            log.debug("HTTP GET failed for {}: {}", url, e.getMessage());
            return null;
        }
    }

    private String extractJsonString(String json, String key) {
        String pattern = "\"" + key + "\"\\s*:\\s*";
        int idx = json.indexOf("\"" + key + "\"");
        if (idx < 0) return null;
        int colonIdx = json.indexOf(":", idx);
        if (colonIdx < 0) return null;
        int valueStart = colonIdx + 1;
        while (valueStart < json.length() && json.charAt(valueStart) == ' ') valueStart++;
        if (valueStart >= json.length()) return null;
        if (json.charAt(valueStart) == '"') {
            int valueEnd = json.indexOf('"', valueStart + 1);
            if (valueEnd < 0) return null;
            return json.substring(valueStart + 1, valueEnd);
        } else if (json.charAt(valueStart) == 'n') {
            return null;
        } else {
            int valueEnd = valueStart;
            while (valueEnd < json.length() && json.charAt(valueEnd) != ',' && json.charAt(valueEnd) != '}') valueEnd++;
            return json.substring(valueStart, valueEnd).trim();
        }
    }

    private List<String> parseTopRepos(String reposJson) {
        List<String> repos = new ArrayList<>();
        int idx = 0;
        while (idx < reposJson.length() && repos.size() < 2) {
            int nameIdx = reposJson.indexOf("\"full_name\"", idx);
            if (nameIdx < 0) break;
            String repoJson = reposJson.substring(nameIdx - 1);
            String name = extractJsonString(repoJson, "full_name");
            String desc = extractJsonString(repoJson, "description");
            String lang = extractJsonString(repoJson, "language");
            String stars = extractJsonString(repoJson, "stargazers_count");
            String forks = extractJsonString(repoJson, "forks_count");
            String updatedAt = extractJsonString(repoJson, "updated_at");
            String pushedAt = extractJsonString(repoJson, "pushed_at");
            StringBuilder entry = new StringBuilder();
            if (name != null) entry.append(name);
            if (lang != null) entry.append(" [").append(lang).append("]");
            if (stars != null && !"0".equals(stars)) entry.append(" ★").append(stars);
            if (forks != null && !"0".equals(forks)) entry.append(" forks=").append(forks);
            if (updatedAt != null) entry.append(" updated=").append(updatedAt.substring(0, Math.min(10, updatedAt.length())));
            if (pushedAt != null) entry.append(" pushed=").append(pushedAt.substring(0, Math.min(10, pushedAt.length())));
            if (desc != null && !desc.isEmpty()) entry.append(" — ").append(desc.length() > 60 ? desc.substring(0, 60) + "..." : desc);
            if (name != null) {
                String languages = fetchRepoLanguages(name);
                if (StringUtils.hasText(languages)) {
                    entry.append("；语言: ").append(languages);
                }
                String files = fetchRootFiles(name);
                if (StringUtils.hasText(files)) {
                    entry.append("；关键文件: ").append(files);
                }
                String commits = fetchRecentCommits(name);
                if (StringUtils.hasText(commits)) {
                    entry.append("；近期提交: ").append(commits);
                }
                String readme = fetchReadmeExcerpt(name);
                if (StringUtils.hasText(readme)) {
                    entry.append("；README摘要: ").append(readme);
                } else {
                    entry.append("；README摘要: 未获取到，不能据此判断项目实现深度");
                }
            }
            if (entry.length() > 0) repos.add(entry.toString());
            idx = nameIdx + 10;
        }
        return repos;
    }

    private String fetchRepoLanguages(String fullName) {
        String json = httpGet("https://api.github.com/repos/" + fullName + "/languages");
        if (!StringUtils.hasText(json) || !json.startsWith("{")) return "";
        List<String> items = new ArrayList<>();
        Matcher matcher = Pattern.compile("\"([^\"]+)\"\\s*:\\s*(\\d+)").matcher(json);
        while (matcher.find() && items.size() < 5) {
            items.add(matcher.group(1) + "=" + matcher.group(2));
        }
        return String.join(", ", items);
    }

    private String fetchRootFiles(String fullName) {
        String json = httpGet("https://api.github.com/repos/" + fullName + "/contents");
        if (!StringUtils.hasText(json) || !json.startsWith("[")) return "";
        List<String> files = new ArrayList<>();
        Matcher matcher = Pattern.compile("\"name\"\\s*:\\s*\"([^\"]+)\"").matcher(json);
        while (matcher.find() && files.size() < 12) {
            String name = matcher.group(1);
            if (name.equalsIgnoreCase("README.md") || name.equalsIgnoreCase("pom.xml")
                    || name.equalsIgnoreCase("package.json") || name.equalsIgnoreCase("Dockerfile")
                    || name.endsWith(".java") || name.endsWith(".py") || name.endsWith(".ts")
                    || name.equalsIgnoreCase("src") || name.equalsIgnoreCase("docs")) {
                files.add(name);
            }
        }
        return String.join(", ", files);
    }

    private String fetchRecentCommits(String fullName) {
        String json = httpGet("https://api.github.com/repos/" + fullName + "/commits?per_page=3");
        if (!StringUtils.hasText(json) || !json.startsWith("[")) return "";
        List<String> commits = new ArrayList<>();
        int idx = 0;
        while (idx < json.length() && commits.size() < 3) {
            int shaIdx = json.indexOf("\"sha\"", idx);
            if (shaIdx < 0) break;
            String segment = json.substring(shaIdx, Math.min(json.length(), shaIdx + 2500));
            String sha = extractJsonString(segment, "sha");
            String message = extractJsonString(segment, "message");
            String date = extractJsonString(segment, "date");
            String item = "";
            if (sha != null && sha.length() >= 7) item += sha.substring(0, 7);
            if (date != null && date.length() >= 10) item += "@" + date.substring(0, 10);
            if (message != null) item += ":" + (message.length() > 80 ? message.substring(0, 80) + "..." : message);
            if (StringUtils.hasText(item)) commits.add(item);
            idx = shaIdx + 5;
        }
        return String.join(" | ", commits);
    }

    private String fetchReadmeExcerpt(String fullName) {
        if (!StringUtils.hasText(fullName) || !fullName.contains("/")) return "";
        String raw = httpGet("https://api.github.com/repos/" + fullName + "/readme", "application/vnd.github.raw");
        if (!StringUtils.hasText(raw)) return "";
        String cleaned = raw
                .replaceAll("(?s)```.*?```", " ")
                .replaceAll("[#>*_`\\[\\]()]"," ")
                .replaceAll("\\s+", " ")
                .trim();
        if (!StringUtils.hasText(cleaned)) return "";
        return cleaned.length() > 260 ? cleaned.substring(0, 260) + "..." : cleaned;
    }
}
