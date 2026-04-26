# 🔐 Blogger Automation Bot v1.0

A fully automated system that scrapes the latest cybersecurity news, translates it into professional Arabic using Google Gemini AI, and publishes it directly to your Blogger site.

---

## 📋 Table of Contents

1. [What Does This Bot Do?](#-what-does-this-bot-do)
2. [Folder Structure](#-folder-structure)
3. [Prerequisites](#-prerequisites)
4. [Step-by-Step Setup Guide](#-step-by-step-setup-guide)
5. [How to Run the Bot](#-how-to-run-the-bot)
6. [How It Works](#-how-it-works)
7. [Troubleshooting](#-troubleshooting)
8. [File Descriptions](#-file-descriptions)

---

## 🌟 What Does This Bot Do?

The bot performs three main tasks automatically:

| Step | Action | Description |
|------|--------|-------------|
| 1️⃣ | **Scrape** | Visits darkwebinformer.com/tag/tools/ and extracts the latest cybersecurity articles |
| 2️⃣ | **Translate** | Sends each article to Google Gemini AI to produce professional Arabic content with HTML formatting |
| 3️⃣ | **Publish** | Posts the translated articles directly to your Blogger blog using the Blogger API |

The bot also prevents duplicate posts by tracking which articles have already been published in a local database file.

---

## 📁 Folder Structure

```
blogger-automation-bot/
│
├── main.py                  # 🚀 The main file you run to start the bot
├── setup.sh                 # 🔧 One-click setup script (run this first!)
├── requirements.txt         # 📦 List of Python packages needed
├── .env.example             # 📝 Template for your API keys
├── .env                     # 🔑 Your actual API keys (you create this)
├── client_secret.json       # 🔑 Google OAuth2 credentials (you download this)
├── README.md                # 📖 This file
│
├── src/                     # 📂 Source code modules
│   ├── __init__.py
│   ├── config.py            # ⚙️ Central configuration (reads from .env)
│   ├── scraper.py           # 🕷️ Web scraping module
│   ├── processor.py         # 🤖 Gemini AI translation module
│   ├── blogger_client.py    # 📤 Blogger API publishing module
│   └── published_db.py      # 💾 Duplicate prevention database
│
├── data/                    # 📂 Data storage (created automatically)
│   ├── published_ids.json   # 📋 Tracks published article URLs
│   └── token.json           # 🔑 Saved OAuth2 login tokens
│
└── logs/                    # 📂 Log files directory
```

---

## ✅ Prerequisites

Before you begin, make sure you have:

- **Python 3.10 or higher** installed on your computer
  - Download from: https://www.python.org/downloads/
  - During installation, check the box that says **"Add Python to PATH"**
- **A Google account** (for both Gemini API and Blogger)
- **A Blogger blog** (create one at: https://www.blogger.com/)
- **Internet connection**

---

## 🚀 Step-by-Step Setup Guide

### Step 1: Download the Project

1. Copy the entire `blogger-automation-bot` folder to your computer
2. Open your terminal or command prompt
3. Navigate to the project folder:
   ```bash
   cd blogger-automation-bot
   ```

### Step 2: Run the Setup Script

This will install all required packages automatically:

```bash
chmod +x setup.sh
./setup.sh
```

If you're on **Windows**, run these commands instead:
```cmd
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
mkdir data logs
copy .env.example .env
```

### Step 3: Get Your Gemini API Key

1. Go to: **https://aistudio.google.com/app/apikey**
2. Click **"Create API Key"**
3. Copy the API key (it looks like: `AIzaSyD...`)
4. Open the `.env` file in any text editor
5. Replace `your_gemini_api_key_here` with your actual key:
   ```
   GEMINI_API_KEY=AIzaSyDzHbCB9uZAd8bw3enMilvsjDaTMCSvRyg
   ```

### Step 4: Find Your Blogger Blog ID

1. Go to: **https://www.blogger.com/**
2. Click on your blog
3. Look at the URL in your browser's address bar. It will look like:
   ```
   https://www.blogger.com/blog/posts/4138913821447265681
   ```
4. The long number at the end is your **Blog ID**
5. Open the `.env` file and paste it:
   ```
   BLOG_ID=4138913821447265681
   ```

### Step 5: Set Up Google Cloud OAuth2 Credentials

This is the most important step. Follow these instructions carefully:

#### 5a. Go to Google Cloud Console

1. Open: **https://console.cloud.google.com/**
2. Sign in with your Google account
3. Click **"Select a project"** at the top, then click **"New Project"**
4. Give it a name (e.g., "Blogger Bot") and click **Create**

#### 5b. Enable the Blogger API

1. In the Google Cloud Console, go to **"APIs & Services" → "Library"**
2. Search for **"Blogger API v3"**
3. Click on it, then click **"Enable"**

#### 5c. Configure the OAuth Consent Screen

1. Go to **"APIs & Services" → "OAuth consent screen"**
2. Choose **"External"** and click **Create**
3. Fill in the required fields:
   - App name: `Blogger Bot`
   - User support email: your email
   - Developer contact email: your email
4. Click **"Save and Continue"** through all steps
5. On the "Scopes" step, click **"Save and Continue"**
6. On the "Test Users" step, **add your own email address** as a test user
7. Click **"Save and Continue"** then **"Back to Dashboard"**

#### 5d. Create OAuth 2.0 Credentials

1. Go to **"APIs & Services" → "Credentials"**
2. Click **"Create Credentials" → "OAuth client ID"**
3. Set:
   - Application type: **"Desktop app"**
   - Name: `Blogger Bot`
4. Click **Create**
5. Click **"Download JSON"** to download your credentials file
6. Rename the downloaded file to **`client_secret.json`**
7. Place it in your project root folder (same folder as `main.py`)

Your project folder should now look like this:
```
blogger-automation-bot/
├── main.py
├── client_secret.json    ← This file!
├── .env
├── requirements.txt
└── src/
    └── ...
```

---

## ▶️ How to Run the Bot

### First Time (One-Time Run)

```bash
# 1. Activate the virtual environment
source venv/bin/activate

# 2. Run the bot once
python main.py
```

On **Windows**:
```cmd
venv\Scripts\activate
python main.py
```

The **first time you run the bot**, it will open a browser window asking you to log into your Google account. This is normal — it needs your permission to post to your blog. After you log in once, it remembers your login for all future runs.

### Continuous Mode (Run Forever)

```bash
python main.py --loop
```

This mode checks for new articles every hour (you can change this in `.env`). Press `Ctrl+C` to stop.

### Expected Output

When the bot runs successfully, you'll see output like this:

```
============================================================
  🔐 BLOGGER AUTOMATION BOT v1.0 🔐
============================================================

✅ Configuration validated successfully!
🤖 Initializing Google Gemini AI...
✅ Gemini AI initialized successfully!
🔐 Found saved login tokens. Loading...
✅ Token is still valid!
📡 Connecting to Blogger API...
✅ Blogger API connected!

============================================================
📰 STEP 1: Scraping articles from source website
============================================================
  🌐 Fetching: https://darkwebinformer.com/tag/tools/
  ✅ Successfully fetched! (Status: 200)
📌 Found 5 article(s) on the page.
✅ Extracted 1200 characters of content
🎉 Successfully scraped 5 article(s) in total!

============================================================
🤖 STEP 2: Translating articles with Gemini AI
============================================================
  ✅ Translation complete!
  📝 Arabic title: ...
  🏷️  Labels: أمن سيبراني, أدوات, حماية
🎉 Successfully translated 3/5 article(s)!

============================================================
📤 STEP 3: Publishing articles to Blogger
============================================================
  ✅ Post published successfully!
  🔗 URL: https://yourblog.blogspot.com/...
🎉 Successfully published 3/3 article(s)!
```

---

## ⚙️ How It Works (Technical Overview)

### Pipeline Flow

```
┌─────────────┐     ┌──────────────┐     ┌────────────────┐     ┌─────────────┐
│   SCRAPE    │────▶│    FILTER    │────▶│   TRANSLATE    │────▶│  PUBLISH    │
│             │     │  (duplicates)│     │   (Gemini AI)  │     │  (Blogger)  │
│  Fetch HTML │     │  Skip already│     │  English→Arabic│     │  Create post│
│  Parse page │     │  published   │     │  Format HTML   │     │  Add labels │
└─────────────┘     └──────────────┘     └────────────────┘     └─────────────┘
```

### HTML Output Format

The AI produces Arabic content with these specific CSS classes:

```html
<p class='pIndent'>المحتوى الرئيسي هنا...</p>

<div class='alert info'>معلومة مهمة: هذه نصيحة أمنية...</div>

<div class='alert warning'>تحذير: هذا الخطر يتطلب الانتباه...</div>
```

### Duplicate Prevention

Every time an article is published, its original URL is saved in `data/published_ids.json`. On subsequent runs, the bot checks this file before processing any article. If the URL is already in the file, the article is skipped.

---

## 🔧 Troubleshooting

### "GEMINI_API_KEY is missing"
- Open `.env` and make sure you pasted your actual Gemini API key
- Make sure there are no spaces around the `=` sign

### "client_secret.json not found"
- Make sure you downloaded the file from Google Cloud Console
- Make sure it's named exactly `client_secret.json` (not `client_secret (1).json`)
- Make sure it's in the same folder as `main.py`

### "Authentication failed" / "Could not authenticate"
- Make sure you added yourself as a Test User in the OAuth consent screen
- Make sure you're running the bot on a machine with a web browser
- Try deleting `data/token.json` and running the bot again to re-authenticate

### "No articles found on the page"
- The source website might be down or have changed its layout
- Try opening the URL in your browser to verify the site is working

### "HTTP Error 403" when scraping
- The website might be blocking automated requests
- Wait a few minutes and try again

### "Rate limited" / HTTP 429
- You're sending too many requests too quickly
- The bot handles this automatically with retries
- If it persists, increase `RETRY_DELAY` in `.env`

### Python version errors
- Make sure you have Python 3.10 or higher
- Check with: `python3 --version`

---

## 📄 File Descriptions

| File | Purpose |
|------|---------|
| `main.py` | The main entry point. Run this to start the bot. Ties all modules together. |
| `setup.sh` | One-click setup script. Installs dependencies and creates directories. |
| `requirements.txt` | Lists all Python packages needed by the bot. |
| `.env.example` | Template for API keys. Copy this to `.env` and fill in your values. |
| `.env` | Your actual API keys. Never share this file or upload it to GitHub. |
| `client_secret.json` | Google OAuth2 credentials downloaded from Google Cloud Console. |
| `src/config.py` | Central configuration. Loads settings from `.env` and provides them to all modules. |
| `src/scraper.py` | Web scraper. Downloads articles from the source website using BeautifulSoup. |
| `src/processor.py` | AI translator. Sends articles to Gemini AI for Arabic translation and HTML formatting. |
| `src/blogger_client.py` | Blogger publisher. Handles Google OAuth2 login and creates blog posts via the API. |
| `src/published_db.py` | Database manager. Tracks published articles in a JSON file to prevent duplicates. |
| `data/published_ids.json` | Auto-generated database of published article URLs. |
| `data/token.json` | Auto-generated file storing your OAuth2 login tokens. |

---

## 📝 Notes

- The bot uses **Gemini 1.5 Flash** for translation, which is fast and cost-effective.
- The bot uses **browser-like HTTP headers** to avoid being blocked by the source website.
- All API calls include **automatic retry logic** with exponential backoff.
- The bot prints **detailed, human-friendly logs** so you always know what it's doing.
- If any step fails (scraping, translation, or publishing), the bot logs the error and continues with the next article — it never crashes silently.

---

**Built with Python, powered by Google Gemini AI and the Blogger API v3.**
