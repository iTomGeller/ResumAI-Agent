package com.resumai.agent.config;

import org.neo4j.driver.AuthTokens;
import org.neo4j.driver.Driver;
import org.neo4j.driver.GraphDatabase;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class Neo4jConfig {

    @Bean(destroyMethod = "close")
    public Driver neo4jDriver(Neo4jProperties props) {
        return GraphDatabase.driver(
                props.getUri(),
                AuthTokens.basic(props.getUsername(), props.getPassword())
        );
    }
}
