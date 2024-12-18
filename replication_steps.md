RUN docker compose file:
```
docker-compose up -d
```

- Connect to the primary database
```
mysql -h 127.0.0.1 -P 3306 -u root -p
```
- Grant replication privileges to the replica user;
```
CREATE USER 'replica'@'%' IDENTIFIED BY 'replica_password';
GRANT REPLICATION SLAVE ON *.* TO 'replica'@'%';
FLUSH PRIVILEGES;
```
- Get the binary log file name and position:
```
SHOW MASTER STATUS;

```
+------------------+----------+--------------+------------------+
| File             | Position | Binlog_Do_DB | Binlog_Ignore_DB |
+------------------+----------+--------------+------------------+
| mysql-bin.000001 |      154 |              |                  |
+------------------+----------+--------------+------------------+

- Connect to the replica instance:
```
mysql -h 127.0.0.1 -P 3307 -u root -p
```
- Configure the replica to connect to the primary:
```
CHANGE MASTER TO
  MASTER_HOST='mysql-container-3306',
  MASTER_USER='replica',
  MASTER_PASSWORD='replica_password',
  MASTER_LOG_FILE='mysql-bin.000001',
  MASTER_LOG_POS=154,
  GET_MASTER_PUBLIC_KEY=1;
```
- Start the replcation process
START SLAVE;
- Check the replica status:
```
SHOW SLAVE STATUS\G;
```
Ensure these values are Yes: Slave_IO_Running: Yes, Slave_SQL_Running: Yes
- Test the replication


