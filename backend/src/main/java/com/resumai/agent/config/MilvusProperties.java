package com.resumai.agent.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "resumai.milvus")
public class MilvusProperties {

    private String host;
    private int port;
    private String collection;
    private String jdCollection = "jd_library";
    private int dimension;

    public String getHost() {
        return host;
    }

    public void setHost(String host) {
        this.host = host;
    }

    public int getPort() {
        return port;
    }

    public void setPort(int port) {
        this.port = port;
    }

    public String getCollection() {
        return collection;
    }

    public void setCollection(String collection) {
        this.collection = collection;
    }

    public String getJdCollection() {
        return jdCollection;
    }

    public void setJdCollection(String jdCollection) {
        this.jdCollection = jdCollection;
    }

    public int getDimension() {
        return dimension;
    }

    public void setDimension(int dimension) {
        this.dimension = dimension;
    }
}
