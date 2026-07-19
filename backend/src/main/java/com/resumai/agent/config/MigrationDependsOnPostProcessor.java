package com.resumai.agent.config;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import org.springframework.beans.factory.config.BeanDefinition;
import org.springframework.beans.factory.config.BeanFactoryPostProcessor;
import org.springframework.beans.factory.config.ConfigurableListableBeanFactory;
import org.springframework.stereotype.Component;

/**
 * Ensures schema migrations run before any MyBatis session (and therefore any
 * mapper-backed @PostConstruct restore logic) touches the database.
 */
@Component
public class MigrationDependsOnPostProcessor implements BeanFactoryPostProcessor {

    @Override
    public void postProcessBeanFactory(ConfigurableListableBeanFactory beanFactory) {
        for (String beanName : new String[]{"sqlSessionFactory", "sqlSessionTemplate"}) {
            if (!beanFactory.containsBeanDefinition(beanName)) {
                continue;
            }
            BeanDefinition definition = beanFactory.getBeanDefinition(beanName);
            List<String> dependsOn = new ArrayList<>();
            if (definition.getDependsOn() != null) {
                dependsOn.addAll(Arrays.asList(definition.getDependsOn()));
            }
            if (!dependsOn.contains("dbMigrationRunner")) {
                dependsOn.add("dbMigrationRunner");
            }
            definition.setDependsOn(dependsOn.toArray(String[]::new));
        }
    }
}
