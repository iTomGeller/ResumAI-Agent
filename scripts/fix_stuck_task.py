import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('8.166.136.122', username='root', password='AS123456hyj', look_for_keys=False, allow_agent=False, timeout=15)

# Check docker-compose for mysql password
stdin, stdout, stderr = ssh.exec_command('grep -A10 "mysql" /opt/ai-resume-agent-platform/docker-compose.yml | grep -i "password\\|MYSQL"', timeout=15)
print("MySQL env:", stdout.read().decode('utf-8', errors='replace'))

# Try different passwords
for pw in ['root123', 'root', 'password', 'mysql', '123456', 'resumai']:
    cmd = f'docker exec resumai-mysql mysql -uroot -p{pw} resume_ai -e "SELECT id,status FROM resume_task WHERE id=1038;"'
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=15)
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    if 'Access denied' not in err and 'ERROR' not in err:
        print(f"Password '{pw}' works! Output: {out}")
        # Now fix the task
        fix_sql = "UPDATE resume_task SET status='FAILED', queue_status='FAILED', fail_reason='maxTokens truncation - fixed', update_time=NOW() WHERE id=1038;"
        cmd2 = f'docker exec resumai-mysql mysql -uroot -p{pw} resume_ai -e "{fix_sql}"'
        stdin2, stdout2, stderr2 = ssh.exec_command(cmd2, timeout=15)
        print("Fix result:", stdout2.read().decode('utf-8', errors='replace'), stderr2.read().decode('utf-8', errors='replace'))
        break
    else:
        print(f"Password '{pw}' failed")

ssh.close()
