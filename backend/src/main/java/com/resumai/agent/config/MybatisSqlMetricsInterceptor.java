package com.resumai.agent.config;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import java.time.Duration;
import java.util.concurrent.TimeUnit;
import org.apache.ibatis.cache.CacheKey;
import org.apache.ibatis.executor.Executor;
import org.apache.ibatis.mapping.BoundSql;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.plugin.Interceptor;
import org.apache.ibatis.plugin.Intercepts;
import org.apache.ibatis.plugin.Invocation;
import org.apache.ibatis.plugin.Plugin;
import org.apache.ibatis.plugin.Signature;
import org.apache.ibatis.session.ResultHandler;
import org.apache.ibatis.session.RowBounds;
import org.springframework.stereotype.Component;

/**
 * 拦截 MyBatis 查询/写入，记录 SQL 耗时、QPS、错误与慢查询（中文业务标签）。
 */
@Component
@Intercepts({
        @Signature(type = Executor.class, method = "update", args = {MappedStatement.class, Object.class}),
        @Signature(type = Executor.class, method = "query", args = {
                MappedStatement.class, Object.class, RowBounds.class, ResultHandler.class
        }),
        @Signature(type = Executor.class, method = "query", args = {
                MappedStatement.class, Object.class, RowBounds.class, ResultHandler.class, CacheKey.class, BoundSql.class
        })
})
public class MybatisSqlMetricsInterceptor implements Interceptor {

    private final MeterRegistry registry;
    private final MysqlObservabilityProperties properties;

    public MybatisSqlMetricsInterceptor(MeterRegistry registry, MysqlObservabilityProperties properties) {
        this.registry = registry;
        this.properties = properties;
    }

    @Override
    public Object intercept(Invocation invocation) throws Throwable {
        if (!properties.isEnabled()) {
            return invocation.proceed();
        }

        MappedStatement mappedStatement = (MappedStatement) invocation.getArgs()[0];
        Object parameter = invocation.getArgs()[1];
        BoundSql boundSql = mappedStatement.getBoundSql(parameter);
        String sql = boundSql.getSql();

        String mapper = MysqlMetricLabels.extractMapper(mappedStatement.getId());
        String method = MysqlMetricLabels.extractMethod(mappedStatement.getId());
        String table = MysqlMetricLabels.extractTable(sql);
        String sqlType = MysqlMetricLabels.extractSqlType(sql);

        long startNanos = System.nanoTime();
        try {
            Object result = invocation.proceed();
            record(mapper, method, table, sqlType, true, null, startNanos);
            return result;
        } catch (Throwable ex) {
            record(mapper, method, table, sqlType, false, ex, startNanos);
            throw ex;
        }
    }

    private void record(
            String mapper,
            String method,
            String table,
            String sqlType,
            boolean success,
            Throwable error,
            long startNanos) {
        long elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - startNanos);
        String mapperCn = MysqlMetricLabels.mapperCn(mapper);
        String methodCn = MysqlMetricLabels.methodCn(method);
        String tableCn = MysqlMetricLabels.tableCn(table);
        String sqlTypeCn = MysqlMetricLabels.sqlTypeCn(sqlType);
        String statusCn = MysqlMetricLabels.statusCn(success);
        String businessCategoryCn = MysqlMetricLabels.businessCategoryCn(table, mapper);

        Timer.builder("resumai.mysql.query.duration")
                .description("MySQL 查询/写入耗时")
                .tag("business_category_cn", businessCategoryCn)
                .tag("mapper_cn", mapperCn)
                .tag("method_cn", methodCn)
                .tag("table_cn", tableCn)
                .tag("sql_type_cn", sqlTypeCn)
                .tag("status_cn", statusCn)
                .register(registry)
                .record(Duration.ofMillis(elapsedMs));

        Counter.builder("resumai.mysql.query.count")
                .description("MySQL 查询/写入次数")
                .tag("business_category_cn", businessCategoryCn)
                .tag("mapper_cn", mapperCn)
                .tag("method_cn", methodCn)
                .tag("table_cn", tableCn)
                .tag("sql_type_cn", sqlTypeCn)
                .tag("status_cn", statusCn)
                .register(registry)
                .increment();

        if (!success) {
            Counter.builder("resumai.mysql.query.error")
                    .description("MySQL 查询/写入错误次数")
                    .tag("business_category_cn", businessCategoryCn)
                    .tag("mapper_cn", mapperCn)
                    .tag("method_cn", methodCn)
                    .tag("table_cn", tableCn)
                    .tag("sql_type_cn", sqlTypeCn)
                    .tag("error_type", errorType(error))
                    .register(registry)
                    .increment();
        }

        if (elapsedMs >= properties.getSlowQueryThresholdMs()) {
            Counter.builder("resumai.mysql.slow.query.count")
                    .description("MySQL 慢查询次数")
                    .tag("business_category_cn", businessCategoryCn)
                    .tag("mapper_cn", mapperCn)
                    .tag("method_cn", methodCn)
                    .tag("table_cn", tableCn)
                    .tag("sql_type_cn", sqlTypeCn)
                    .register(registry)
                    .increment();
        }
    }

    private static String errorType(Throwable error) {
        if (error == null) {
            return "SQL_ERROR";
        }
        return error.getClass().getSimpleName();
    }

    @Override
    public Object plugin(Object target) {
        return Plugin.wrap(target, this);
    }
}
