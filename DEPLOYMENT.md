# Deploying BridgeText to Render

## Prerequisites

1. GitHub account
2. Render account (free tier works) - https://render.com
3. Your API keys ready (OpenAI, Meta/Twilio, Google)

## Step-by-Step Deployment

### 1. Push code to GitHub

If you haven't already:

```cmd
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/Sufi-san/BridgeText.git
git push -u origin main
```

⚠️ **Important:** Make sure `.env` is in `.gitignore` so secrets don't get pushed!

### 2. Create a new Web Service on Render

1. Go to https://render.com/dashboard
2. Click "New +" → "Web Service"
3. Connect your GitHub account
4. Select the `BridgeText` repository
5. Configure the service:

**Basic settings:**
- **Name:** `bridgetext-bot` (or any name you want)
- **Region:** Choose closest to you
- **Branch:** `main`
- **Root Directory:** (leave empty)
- **Runtime:** `Python 3`
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`

**Instance Type:**
- Select "Free" for testing (spins down after inactivity)
- Or "Starter" ($7/month) for always-on production

### 3. Add Environment Variables on Render

In the Render dashboard for your service:

1. Scroll to "Environment Variables" section
2. Click "Add Environment Variable"
3. Add each of these (click "Add" after each):

```
OPENAI_API_KEY = your_openai_key_here
GOOGLE_API_KEY = your_google_key_here
TWILIO_ACCOUNT_SID = your_twilio_sid_here
TWILIO_AUTH_TOKEN = your_twilio_token_here
TWILIO_VALIDATE = true
META_ACCESS_TOKEN = your_meta_token_here
META_PHONE_NUMBER_ID = your_meta_phone_id_here
META_VERIFY_TOKEN = stepbot_verify
DEBUG_SAVE_MEDIA = false
PORT = 5000
FLASK_DEBUG = false
LOG_LEVEL = INFO
```

⚠️ Only add the keys you actually have. If using only Meta, skip Twilio vars.

### 4. Deploy

1. Click "Create Web Service"
2. Render will:
   - Clone your repo
   - Install dependencies
   - Start the app
3. Wait for "Your service is live 🎉" message
4. Copy your app URL: `https://bridgetext-bot.onrender.com`

### 5. Configure Webhooks

#### For Meta/WhatsApp Cloud:

1. Go to Meta Developer Portal → Your App → WhatsApp → Configuration
2. Set Callback URL: `https://bridgetext-bot.onrender.com/meta-webhook`
3. Set Verify Token: `stepbot_verify`
4. Subscribe to webhook fields: `messages`

#### For Twilio:

1. Go to Twilio Console → Messaging → Settings → WhatsApp Sandbox
2. Set "When a message comes in" webhook:
   - URL: `https://bridgetext-bot.onrender.com/whatsapp-webhook`
   - HTTP Method: POST

### 6. Test Your Bot

Send a WhatsApp message to your bot number. Check Render logs to see if webhook is triggered:

- In Render dashboard → your service → "Logs" tab
- You should see incoming requests logged

## Troubleshooting

### Logs show "vector store not found"
- This is normal if you haven't uploaded `my_vector_store` folder
- App will fall back to direct OpenAI chat (still works!)

### "Module not found" errors
- Check `requirements.txt` includes all dependencies
- Trigger a manual deploy in Render dashboard

### Webhook verification fails (Meta)
- Check `META_VERIFY_TOKEN` matches what you set in Meta portal
- Check callback URL is exactly `https://your-app.onrender.com/meta-webhook`

### Free tier sleeps after inactivity
- Render free tier spins down after 15 min inactivity
- First message after sleep takes ~30 seconds to wake up
- Upgrade to paid tier ($7/mo) for always-on

## Alternative: Deploy to other platforms

This app can also deploy to:
- **Railway:** Similar to Render, $5/month
- **Heroku:** $5-7/month (no free tier anymore)
- **Google Cloud Run:** Pay-per-use
- **AWS Lambda + API Gateway:** Serverless option

Same steps apply - just add environment variables and point webhooks to your deployed URL.

## Notes on `my_vector_store`

If you want RAG functionality with your existing vector store:

1. Commit `my_vector_store` folder to git (if files aren't too large)
2. Or upload to cloud storage (S3, Google Cloud Storage) and download on startup
3. Or rebuild on first deploy using `python ingestion.py` (requires PDFs in repo)

For simplicity, the app works fine without it - just won't have RAG grounding.
