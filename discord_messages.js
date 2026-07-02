const { Client, GatewayIntentBits, ChannelType } = require("discord.js");
const fs = require("fs");

const client = new Client({
    intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.MessageContent
    ]
});

const CSV_FILE = "discord_messages.csv";

client.once("ready", async () => {
    console.log(`Logged in as ${client.user.tag}`);

    // 1. Create or clear the file, and write the header right away
    const headers = [
        "Server", "Channel", "Message ID", "Timestamp", "Display Name",
        "Avatar URL", "User ID", "Content", "Attachments", "Reply To", "Edited", "Pinned"
    ].join(",");
    fs.writeFileSync(CSV_FILE, headers + "\n", "utf8");

    let totalExported = 0;

    for (const guild of client.guilds.cache.values()) {
        console.log(`\nServer: ${guild.name}`);
        await guild.channels.fetch();

        for (const channel of guild.channels.cache.values()) {
            if (channel.type !== ChannelType.GuildText) continue;

            console.log(`Reading #${channel.name}`);

            try {
                // 2. Fetch and append data sequentially down to the disk
                const count = await fetchAndAppendMessages(guild, channel);
                totalExported += count;
            } catch (err) {
                console.error(`Critical failure on #${channel.name}: ${err.message}`);
            }
        }
    }

    console.log(`\nFinished! Total successfully exported: ${totalExported} messages.`);
    client.destroy();
});

async function fetchAndAppendMessages(guild, channel) {
    let lastId;
    let channelCount = 0;

    while (true) {
        const options = { limit: 100 };
        if (lastId) options.before = lastId;

        let messages;
        try {
            messages = await channel.messages.fetch(options);
        } catch (fetchError) {
            console.error(`\n[Warning] Network/Rate limit error on #${channel.name} after ${channelCount} messages: ${fetchError.message}`);
            break; // Stop fetching this channel but keep what was already written to disk
        }

        if (messages.size === 0) break;

        // Convert this specific 100-message chunk directly into CSV text strings
        const rows = [];
        for (const msg of messages.values()) {
            rows.push([
                escapeCSV(guild.name),
                escapeCSV(channel.name),
                escapeCSV(msg.id),
                escapeCSV(msg.createdAt?.toISOString() ?? ""),
                escapeCSV(msg.member?.displayName ?? msg.author?.globalName ?? msg.author?.username ?? ""),
                escapeCSV(msg.author?.displayAvatarURL({ extension: "png", size: 512 }) ?? ""),
                escapeCSV(msg.author?.id),
                escapeCSV(msg.content),
                escapeCSV(msg.attachments?.map(a => a.url).join("; ") ?? ""),
                escapeCSV(msg.reference?.messageId ?? ""),
                escapeCSV(msg.editedAt ? "Yes" : "No"),
                escapeCSV(msg.pinned)
            ].join(","));
        }

        // 3. Dump the chunk straight to the file immediately
        fs.appendFileSync(CSV_FILE, rows.join("\n") + "\n", "utf8");

        channelCount += messages.size;
        lastId = messages.last().id;

        console.log(`   Fetched and saved ${channelCount} messages...`);

        // Give the API a brief breather to actively avoid rate limits
        await new Promise(resolve => setTimeout(resolve, 200));
    }

    return channelCount;
}

function escapeCSV(value) {
    if (value === undefined || value === null) return "";
    return `"${String(value).replace(/"/g, '""')}"`;
}

client.login(process.env.DISCORD_API_KEY);