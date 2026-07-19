FROM docker.m.daocloud.io/library/maven:3.9.8-eclipse-temurin-21 AS builder
WORKDIR /workspace
COPY settings.xml /root/.m2/settings.xml
COPY pom.xml .
RUN mvn -B -s /root/.m2/settings.xml dependency:go-offline
COPY src ./src
RUN mvn -B -s /root/.m2/settings.xml clean verify

FROM docker.m.daocloud.io/library/eclipse-temurin:21-jre
WORKDIR /app
ENV TZ=Asia/Shanghai
RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g; s|http://security.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/*.sources 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /workspace/target/*.jar /app/resumai-agent-backend.jar
COPY src/main/resources/skills/ /app/skills/
COPY src/main/resources/mcp-servers.json /app/mcp-servers.json
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/resumai-agent-backend.jar"]
