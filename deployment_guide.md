# BlueDart Popup Backend Deployment Guide

Run these commands sequentially on your production server to deploy the application.

## 1. Install & Setup Environment
```bash
# Navigate to project directory
cd /path/to/bluedart_popup

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## 2. Configure Systemd Service
```bash
# Copy the template service file to systemd directory
sudo cp bluedart-popup.service /etc/systemd/system/

# Open the file and update User, WorkingDirectory, and ExecStart paths
sudo nano /etc/systemd/system/bluedart-popup.service
```

## 3. Enable & Start Application
```bash
# Reload systemd to recognize the new service
sudo systemctl daemon-reload

# Enable service to start automatically on server reboot
sudo systemctl enable bluedart-popup

# Start the service immediately
sudo systemctl start bluedart-popup
```

## 4. Verify & View Logs
```bash
# Check if the service is running successfully
sudo systemctl status bluedart-popup

# View real-time application logs
tail -f app.log

# View system-level daemon errors (if it fails to start)
sudo journalctl -u bluedart-popup -f
```

## 5. Verify the API is Live

Once the service is running, you can test if the API is correctly exposed to the network. Open your web browser and navigate to:

1. **Health Check endpoint:**
   `http://<YOUR_SERVER_IP>:8000/api/health`
   *(Should return `{"connected": true, ...}`)*

2. **Interactive API Documentation (Swagger UI):**
   `http://<YOUR_SERVER_IP>:8000/docs`
   *(This gives you a visual interface to see and test the `login-popup-summary` and `ask-ai` endpoints directly from your browser!)*
