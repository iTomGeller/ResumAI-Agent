package com.resumai.agent.config;

import com.resumai.agent.util.HrContext;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;
import org.springframework.web.filter.OncePerRequestFilter;

@Component
@Order(Ordered.HIGHEST_PRECEDENCE + 20)
public class HrContextFilter extends OncePerRequestFilter {

    public static final String HR_ID_HEADER = "X-HR-Id";

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain filterChain)
            throws ServletException, IOException {
        try {
            String hrId = request.getHeader(HR_ID_HEADER);
            if (StringUtils.hasText(hrId)) {
                HrContext.setHrId(hrId.trim());
            }
            filterChain.doFilter(request, response);
        } finally {
            HrContext.clear();
        }
    }
}
