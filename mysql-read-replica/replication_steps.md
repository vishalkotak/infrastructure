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
# Here % represents a wildcard that represents any host. It allows the user 
# replica to connect to the MySQL server from any IP address or hostname.
CREATE USER 'replica'@'%' IDENTIFIED BY 'replica_password';

# When a user is given the REPLICATION SLAVE privilege:
# 1. The user can connect to the primary server to fetch its binary log files for replication.
# The user can read the binary log to apply transactions on the replica server.
# The user cannot modify or read data directly from the database tables.
GRANT REPLICATION SLAVE ON *.* TO 'replica'@'%';

FLUSH PRIVILEGES;
```
- Get the binary log file name and position:
```
SHOW MASTER STATUS;
```
- Connect to the replica instance:
```
mysql -h 127.0.0.1 -P 3307 -u root -p
```
- Configure the replica to connect to the primary:
```
CHANGE MASTER TO
  MASTER_HOST='secondary-mysql-container',
  MASTER_USER='replica',
  MASTER_PASSWORD='replica_password',
  MASTER_LOG_FILE='mysql-bin.000001',
  MASTER_LOG_POS=154,
  GET_MASTER_PUBLIC_KEY=1;
```
- Start the replcation process
```
START SLAVE;
```
- Check the replica status:
```
SHOW SLAVE STATUS\G;
```
Ensure these values are Yes: Slave_IO_Running: Yes, Slave_SQL_Running: Yes
- Test the replication


