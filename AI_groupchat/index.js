require("dotenv").config();
const fs = require("fs");
const { Client, GatewayIntentBits } = require("discord.js");
const { GoogleGenerativeAI } = require("@google/generative-ai");

const botsConfig = JSON.parse(fs.readFileSync("./config.json", "utf-8"));

process.on("unhandledRejection", (err) => {
    console.error("UNHANDLED REJECTION:", err);
    process.exit(1);
});

process.on("uncaughtException", (err) => {
    console.error("UNCAUGHT EXCEPTION:", err);
    process.exit(1);
});

function serializeMessage(msg) {
    const referencedMessage = msg.reference?.messageId ? msg.channel.messages.cache.get(msg.reference.messageId) : null;
    return {
        message_id: msg.id,
        timestamp: msg.createdAt.toISOString(),

        author: {
            author_id: msg.author.id,
            username: msg.author.username,
            bot: msg.author.bot
        },

        message_content: msg.content,

        attachments: [...msg.attachments.values()].map(a => ({
            name: a.name,
            url: a.url
        })),

        embeds: msg.embeds,

        reactions: [...msg.reactions.cache.values()].map(r => ({
            emoji: r.emoji.name,
            count: r.count
        })),

        mentions: {
            users: msg.mentions.users.map(u => u.username),
            roles: msg.mentions.roles.map(r => r.name)
        },

        replyTo: msg.reference?.messageId,
        // Add the author info if the referenced message is in cache
        replyToAuthor: referencedMessage ? referencedMessage.author.username : null
    };
}

function getCurrentSociability(client, phase) {
    const now = Date.now();
    const cycleLength = 6 * 60 * 60 * 1000; //6 hours
    const mood =((1 + Math.sin(2 * Math.PI * now / cycleLength + phase)) / 2.14)**10;
    let status;
    if (mood < 0.01) status = "invisible";
    else if (mood < 0.1) status = "dnd";
    else if (mood < 0.3) status = "idle";
    else status = "online";
    client.user.setPresence({
        status
    });
    return mood
}

function createBot(config) {
    const genAI = new GoogleGenerativeAI(config.GEMINI_API_KEY);

    const model = genAI.getGenerativeModel({
        model: "gemini-3.1-flash-lite",
        systemInstruction:
            `You are a Discord user named ${config.name}.\n` +
            config.systemInstruction
    });

    const client = new Client({
        intents: [
            GatewayIntentBits.Guilds,
            GatewayIntentBits.GuildMessages,
            GatewayIntentBits.MessageContent,
        ],
    });

    const phase = Math.random() * 2 * Math.PI;

    const pendingResponses = new Map();
    

    client.on("messageCreate", async (message) => {
        const channelId = message.channel.id;

        if (pendingResponses.has(channelId)) {
            clearTimeout(pendingResponses.get(channelId));
        }

        pendingResponses.set(
            channelId,
            setTimeout(async () => {
                pendingResponses.delete(channelId);
                if (Math.random() > getCurrentSociability(client, phase)) return;
                if (message.author.id === client.user.id) return;

                const messages = await message.channel.messages.fetch({
                    limit: config.memorySize
                });

                const history = [...messages.values()]
                .reverse()
                .map(serializeMessage);

                try {
                    const conversation = JSON.stringify(history.slice(0,history.length-1))
                    //console.log(conversation+'\n\n');

                    const newest = history[history.length - 1];
                    if (!newest) return;
                    //console.log(JSON.stringify(newest)+'\n\n\n\n');

                    const newestMessage = JSON.stringify(newest);
                    
                    const result = await model.generateContent(`
                    RECENT CONVERSATION:
                    ${conversation}

                    New message:
                    ${newestMessage}

                    Carefully decide whether to respond by analyzing if you are addressed in the new message by using context and replyToAuthor tag. (It's not encouraged to break into the conversation if you are not addressed)

                    If you should NOT respond, output exactly:
                    NO_REPLY

                    If you SHOULD respond:
                    - respond following system instructions
                    - carefully analyze conversation context from RECENT CONVERSATION using all the metadata given in the conversation history
                    - do NOT repeat what was already said in the conversation. Your response should be unique and creative

                    If your response is supposed to be the direct reply towards the new message, end with REPLY_T
                    otherwise end with REPLY_F
                    ` + (config.responseInstruction || "")
                    );

                    const response = await result.response;
                    let text = response.text().trim();

                    if (text === "NO_REPLY") return;

                    const isTarget = text.endsWith("REPLY_T");
                    text = text.replace(/REPLY_[TF]$/, "").trim();

                        await message.channel.sendTyping();

                        if (isTarget) {
                            await message.reply(text);
                        } else {
                            await message.channel.send(text);
                        }

                } catch (err) {
                    console.error(`[${config.name}] error:`, err);
                }
            }, 3000)
        );
    });

    client.once("clientReady", () => {
        console.log(`${config.name} logged in`);
        startRandomChat(client, model, config, phase);
    });

    client.login(config.DISCORD_TOKEN);
}


async function startRandomChat(client, model, config, phase) {
    setInterval(async () => {

        try {
            const guilds = client.guilds.cache;

            for (const [, guild] of guilds) {
                const channels = guild.channels.cache
                    .filter(c =>
                        c.isTextBased() &&
                        c.permissionsFor(client.user).has("SendMessages")
                    );

                // pick a random channel
                const randomChannel =
                    channels.random();

                if (!randomChannel) continue;

                // mood gate
                const mood = getCurrentSociability(client, phase);
                if (Math.random() > mood) continue;

                const messages = await randomChannel.messages.fetch({
                    limit: config.memorySize
                });
                // fetch history of conversation
                const history = [...messages.values()]
                    .reverse()
                    .map(serializeMessage);

                const conversation = JSON.stringify(history);

                const result = await model.generateContent(`
                RECENT CONVERSATION for the reference:
                ${conversation}

                PROMPT:
                You are ${config.name}.
                Start a short natural message that's relevant to the conversation above, and can keep the conversation going. You can introduce new topics. Follow the system instructions strictly.
                
                When you write a message:
                - Carefully analyze conversation context from RECENT CONVERSATION using all the metadata given in the conversation history.
                - Do NOT add any system information like username or timestamp in the beginning of your response.
                - Do NOT repeat what was already said in the conversation. Your response should be unique and creative.
                `);

                const response = await result.response;
                const text = response.text().trim();

                await randomChannel.send(text);

            }

        } catch (err) {
            console.error(`[${config.name}] random chat error:`, err);
        }

    }, 60 * 1000 * (1 + Math.random() * 4)); // 1–5 min
}


for (const config of botsConfig) {
    createBot(config);
}