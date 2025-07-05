# 🌙 Crescent

**Self-hosted hosting finance dashboard for Pterodactyl + Paymenter**

Crescent is an open-source dashboard for managing and visualizing your hosting income, costs, and server usage built to work seamlessly with [Pterodactyl](https://pterodactyl.io) and [Paymenter](https://github.com/Paymenter/Paymenter).

> 💸 Track node-linked income
> 💻 Monitor usage like Grafana
> 🧾 Add and sort custom expenses

---

## 🚀 Pages

- 📊 **Dashboard**: Overall income and key metrics at a glance.
- 💰 **Incomings**: Detailed income data fetched from Paymenter, organized by node.
- 💸 **Outgoings**: Easily add and manage expenses, categorized by node or as miscellaneous costs.
- 📈 **Machine Usage**: Grafana-style graphs providing insights into RAM and storage utilization.
- ⚙️ **Settings**: Personalize your account options and configure 2-Factor Authentication for enhanced security.
- 🔐 **Admin**: Powerful user management interface with role-based access control.

---

## 🌐 Links

- 🌐 Demo: [crescent.exphost.net](https://crescent.exphost.net)
- 📚 Docs: [docs.exphost.net/](https://docs.exphost.net/)
- 🧰 Install Scripts: [billing.exphost.net](https://billing.exphost.net)

---

## 💾 Installation Guide

This guide will walk you through the manual installation of Crescent on a Linux system.

### Install Dependencies:
```bash
sudo apt update
sudo apt install python3 python3-pip nginx
````

### Installation:

#### 1\. Create a directory for Crescent and navigate into it:

```bash
mkdir /var/www/crescent
cd /var/www/crescent
```

#### 2\. Download the latest release:

```bash
curl -Lo crescent.tar.gz https://github.com/exphost-net/Crescent/releases/latest/download/crescent.tar.gz
```

#### 3\. Extract the downloaded archive:

```bash
tar -xzvf crescent.tar.gz
```

#### 4\. Install Python dependencies:

```bash
pip install -r requirements.txt
```

#### 5\. Configure Database Access for Pterodactyl:

First, access the MySQL command line interface on the machine hosting your Pterodactyl panel:

```bash
sudo mysql -u root -p
```

Then, create a dedicated user for Crescent with the necessary permissions. Replace `your_crescent_host_ip` with the IP address of the machine where Crescent is being installed. If Crescent is on the same machine as Pterodactyl, you can use `localhost` or `127.0.0.1`. Remember to choose a strong password instead of `secure-password`.

```sql
CREATE USER 'crescent'@'your_crescent_host_ip' IDENTIFIED BY 'secure-password';
GRANT SELECT ON panel.* TO 'crescent'@'your_crescent_host_ip';
FLUSH PRIVILEGES;
QUIT;
```

#### 6\. Configure Database Access for Paymenter:

Similarly, access the MySQL command line interface on the machine hosting your Paymenter installation:

```bash
sudo mysql -u root -p
```

Create a dedicated user for Crescent. Again, replace `your_crescent_host_ip` with the IP address of the Crescent server (use `localhost` or `127.0.0.1` if it's the same as Paymenter) and choose a secure password.

```sql
CREATE USER 'crescent'@'your_crescent_host_ip' IDENTIFIED BY 'secure-password';
GRANT SELECT ON paymenter.* TO 'crescent'@'your_crescent_host_ip';
FLUSH PRIVILEGES;
QUIT;
```

#### 7\. Configure Environment Variables:

Copy the example environment file:

```bash
cp .env.example .env
```

Now, edit the `.env` file to include your database credentials, Pterodactyl API details, and the default admin user information:

```
PAYMENTER_DB_USER=crescent
PAYMENTER_DB_PASSWORD=secure-password
PAYMENTER_DB_HOST=your_paymenter_host_ip
PAYMENTER_DB_NAME=paymenter

PTERODACTYL_DB_USER=crescent
PTERODACTYL_DB_PASSWORD=secure-password
PTERODACTYL_DB_HOST=your_pterodactyl_host_ip
PTERODACTYL_DB_NAME=panel

PTERODACTYL_API_URL=https://panel.example.com/
PTERODACTYL_API_KEY=ptero-api

DEFAULT_ADMIN_EMAIL=admin@example.com
DEFAULT_ADMIN_PASSWORD=ChangeMe123
```

**Important:** Replace the placeholder values with your actual configuration, and make sure to **change the admin password**.

#### 8\. Set File Permissions:

```bash
sudo chown -R www-data:www-data /var/www/crescent
```

#### 9\. Create a Systemd Service File:

Create a service definition for Crescent:

```bash
sudo nano /etc/systemd/system/crescent.service
```

Paste the following content into the file:

```ini
[Unit]
Description=Crescent Backend
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/var/www/crescent
ExecStart=/usr/bin/python3 /var/www/crescent/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Save and close the file (`CTRL+X`, then `y`, then `Enter`).

#### 10\. Enable and Start the Crescent Service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable crescent.service
sudo systemctl start crescent.service
```

-----

## 🌐 Webserver Setup (Nginx)

This section guides you through configuring Nginx to serve the Crescent application.

### With a Domain & HTTP:

1.  **DNS Configuration:** Create an A record for your desired subdomain (e.g., `crescent.example.com`) pointing to the IP address of your server. If using Cloudflare, ensure the proxy (orange cloud) is disabled.

2.  Create the Nginx configuration file:

    ```bash
    sudo nano /etc/nginx/sites-available/crescent.conf
    ```

3.  Paste the following configuration, replacing `crescent.example.com` with your actual domain or subdomain:

    ```nginx
    server {
        listen 80 default_server;
        listen [::]:80 default_server;

        server_name crescent.example.com;

        location / {
            proxy_pass http://127.0.0.1:5000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }
    ```

    Save the file (`CTRL+X`, then `y`, then `Enter`).

4.  Enable the configuration:

    ```bash
    sudo ln -s /etc/nginx/sites-available/crescent.conf /etc/nginx/sites-enabled/crescent.conf
    ```

5.  Restart Nginx:

    ```bash
    sudo systemctl restart nginx
    ```

    You can now access Crescent at `http://crescent.example.com:80` or simply `http://crescent.example.com` if port 80 is the default.

### With a Domain & HTTPS (SSL Certificates):

1.  **DNS Configuration:** Create an A record for your desired subdomain (e.g., `crescent.example.com`) pointing to the IP address of your server. If using Cloudflare, ensure the proxy (orange cloud) is disabled.

2.  **Install Certbot:** Install Certbot and the Nginx plugin for automatic SSL certificate management:

    ```bash
    sudo apt install certbot python3-certbot-nginx
    ```

3.  **(Optional) Auto-Renewal:** To set up automatic renewal of your SSL certificates, edit the crontab:

    ```bash
    sudo crontab -e
    ```

    Add the following line (if it's not already present):

    ```cron
    0 23 * * * certbot renew --quiet --deploy-hook "systemctl restart nginx"
    ```

    Save and close the crontab.

4.  **Obtain SSL Certificates:** Request SSL certificates for your domain using Certbot. Replace `crescent.example.com` with your actual domain:

    ```bash
    sudo certbot --nginx -d crescent.example.com
    ```

    Follow the prompts to complete the certificate issuance process.

5.  Create or edit the Nginx configuration file:

    ```bash
    sudo nano /etc/nginx/sites-available/crescent.conf
    ```

6.  Paste the following configuration, ensuring you replace `crescent.example.com` with your actual domain:

    ```nginx
    server {
        listen 80 default_server;
        listen [::]:80 default_server;
        server_name crescent.example.com;
        return 301 https://$host$request_uri;
    }

    server {
        listen 443 ssl default_server;
        listen [::]:443 ssl default_server;

        server_name crescent.example.com;

        ssl_certificate /etc/letsencrypt/live/crescent.example.com/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt/live/crescent.example.com/privkey.pem;

        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_prefer_server_ciphers on;
        ssl_ciphers 'ECDHE+AESGCM:CHACHA20';
        ssl_ecdh_curve secp384r1;

        location / {
            proxy_pass http://127.0.0.1:5000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }
    ```

    Save the file (`CTRL+X`, then `y`, then `Enter`).

7.  Enable the configuration:

    ```bash
    sudo ln -s /etc/nginx/sites-available/crescent.conf /etc/nginx/sites-enabled/crescent.conf
    ```

8.  Restart Nginx:

    ```bash
    sudo systemctl restart nginx
    ```

    You can now access Crescent securely at `https://crescent.example.com`.

-----

## 🔄 Updating Crescent

To update Crescent to the latest version, follow these steps:

1. **Download the latest release**:
   ```bash
   curl -Lo crescent.tar.gz https://github.com/exphost-net/Crescent/releases/latest/download/crescent.tar.gz
   ```

2. **Extract the downloaded archive**:
   ```bash
   tar -xzvf crescent.tar.gz
   ```

3. **Install updated Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Restart the Crescent service**:
   ```bash
   sudo systemctl restart crescent.service
   ```

That's it! Crescent should now be updated to the latest version.

---

## 🛠 Tech Stack

  * Python / Flask (backend)
  * HTML/CSS/JS (frontend)
  * API integrations (Pterodactyl, Paymenter)

-----

## 📝 License

This project is licensed under the [GPL-3.0 License](https://www.google.com/search?q=LICENSE).

-----

## 🤝 Credits

Created by [ExpHost](https://www.exphost.net)
Maintained by the ExpHost Team - [crescent.exphost.net](https://crescent.exphost.net)
