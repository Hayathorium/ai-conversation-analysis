# Discord AI Personality Bot

A modular Discord bot framework powered by **Google Gemini** that simulates natural, personality-driven human interaction. Unlike standard assistant bots, this bot uses mood-based scheduling, conversation history analysis, and context-aware triggers to behave like a regular Discord user.

## Features

* **Human-Like Interaction**: Configurable personality instructions to ensure the bot sounds like a person, not an AI.
* **Mood & Sociability Engine**: Uses a sine-wave algorithm to cycle through moods, determining when the bot is "online," "idle," or "dnd" to mimic human activity patterns.
* **Context-Aware**: Processes conversation metadata (timestamps, usernames, and reply chains) to decide if and how it should respond.
* **Proactive**: Periodically initiates conversations in random channels to keep engagement high.
* **Low Maintenance**: Designed to be run 24/7 using `pm2` and Node.js.

## Live Demo

Come hang out with the bots! Our instances are running 24/7. Everyone is welcome to join the server and interact with the AI personalities:

👉 **[Join the Discord Server](https://disboard.org/server/1509781747338973194)**

## Getting Started

### Prerequisites

* [Node.js](https://nodejs.org/) installed.
* A Discord Bot Token from the [Discord Developer Portal](https://discord.com/developers/).
* A Google Gemini API Key from [Google AI Studio](https://ai.google.dev/gemini-api/).
* `pm2` (recommended for 24/7 hosting).

### Installation

1. **Clone the repository:**
```bash
git clone https://github.com/yourusername/your-repo-name.git
cd your-repo-name

```


2. **Install dependencies:**
```bash
npm install

```


3. **Configure the bots:**
Edit the `config.json` file to add your credentials and personality settings:
```json
[
  {
    "name": "BotName",
    "DISCORD_TOKEN": "YOUR_DISCORD_TOKEN",
    "GEMINI_API_KEY": "YOUR_GEMINI_KEY",
    "memorySize": 30,
    "systemInstruction": "Your personality description here...",
    "responseInstruction": "Optional specific behavioral constraints"
  }
]

```


4. **Run with PM2:**
```bash
pm2 start index.js --name "discord-ai-bots"

```
