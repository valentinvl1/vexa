import StealthPlugin from "puppeteer-extra-plugin-stealth";
import { log } from "./utils";
import { chromium } from "playwright-extra";
import { handleGoogleMeet } from "./platforms/google";
import { browserArgs, userAgent } from "./constans";
import { BotConfig } from "./types";
import { createClient, RedisClientType } from 'redis';

// Module-level variables to store current configuration
let currentLanguage: string | null | undefined = null;
let currentTask: string | null | undefined = 'transcribe'; // Default task
let currentRedisUrl: string | null = null;
let currentConnectionId: string | null = null;

// --- ADDED: Flag to prevent multiple shutdowns ---
let isShuttingDown = false;
// ---------------------------------------------

// --- ADDED: Redis subscriber client ---
let redisSubscriber: RedisClientType | null = null;
// -----------------------------------

// --- ADDED: Message Handler ---
const handleRedisMessage = (message: string, channel: string) => {
  log(`Received command on ${channel}: ${message}`);
  // TODO: Implement actual command handling (Phase 3 & 4)
};
// ----------------------------

export async function runBot(botConfig: BotConfig): Promise<void> {
  // --- UPDATED: Parse and store config values ---
  currentLanguage = botConfig.language;
  currentTask = botConfig.task || 'transcribe'; // Use default if null/undefined
  currentRedisUrl = botConfig.redisUrl;
  currentConnectionId = botConfig.connectionId;
  // ---------------------------------------------

  // Destructure other needed config values
  const { meetingUrl, platform, botName } = botConfig;

  log(`Starting bot for ${platform} with URL: ${meetingUrl}, name: ${botName}, language: ${currentLanguage}, task: ${currentTask}, connectionId: ${currentConnectionId}`);

  // --- ADDED: Redis Client Setup and Subscription ---
  if (currentRedisUrl && currentConnectionId) {
    log("Setting up Redis subscriber...");
    try {
      redisSubscriber = createClient({ url: currentRedisUrl });

      redisSubscriber.on('error', (err) => log(`Redis Client Error: ${err}`));

      await redisSubscriber.connect();
      log(`Connected to Redis at ${currentRedisUrl}`);

      const commandChannel = `bot_commands:${currentConnectionId}`;
      await redisSubscriber.subscribe(commandChannel, handleRedisMessage);
      log(`Subscribed to Redis channel: ${commandChannel}`);

    } catch (err) {
      log(`*** Failed to connect or subscribe to Redis: ${err} ***`);
      // Decide how to handle this - exit? proceed without command support?
      // For now, log the error and proceed without Redis.
      redisSubscriber = null; // Ensure client is null if setup failed
    }
  } else {
    log("Redis URL or Connection ID missing, skipping Redis setup.");
  }
  // -------------------------------------------------

  // Use Stealth Plugin to avoid detection
  const stealthPlugin = StealthPlugin();
  stealthPlugin.enabledEvasions.delete("iframe.contentWindow");
  stealthPlugin.enabledEvasions.delete("media.codecs");
  chromium.use(stealthPlugin);

  // Launch browser with stealth configuration
  const browser = await chromium.launch({
    headless: false,
    args: browserArgs,
  });

  // Create a new page with permissions and viewport
  const context = await browser.newContext({
    permissions: ["camera", "microphone"],
    userAgent: userAgent,
    viewport: {
      width: 1280,
      height: 720
    }
  })
  const page = await context.newPage();

  // Setup anti-detection measures
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
    Object.defineProperty(navigator, "plugins", {
      get: () => [{ name: "Chrome PDF Plugin" }, { name: "Chrome PDF Viewer" }],
    });
    Object.defineProperty(navigator, "languages", {
      get: () => ["en-US", "en"],
    });
    Object.defineProperty(navigator, "hardwareConcurrency", { get: () => 4 });
    Object.defineProperty(navigator, "deviceMemory", { get: () => 8 });
    Object.defineProperty(window, "innerWidth", { get: () => 1920 });
    Object.defineProperty(window, "innerHeight", { get: () => 1080 });
    Object.defineProperty(window, "outerWidth", { get: () => 1920 });
    Object.defineProperty(window, "outerHeight", { get: () => 1080 });
  });

  // Switch based on the *external* platform name received in botConfig
  switch (platform) {
    case 'google_meet': // Use external name
      await handleGoogleMeet(botConfig, page)
      break;
    case 'zoom': // External name
      // todo
      //await handleMeet(page);
      break;
    case 'teams': // External name
      // todo
      //  await handleTeams(page);
      break;
    default:
      // Log the unexpected platform value
      log(`Error: Unsupported platform received: ${platform}`);
      throw new Error(`Unsupported platform: ${platform}`);
  }

  // --- ADDED: Close Redis connection on exit ---
  if (redisSubscriber && redisSubscriber.isOpen) {
    log("Disconnecting Redis subscriber...");
    try {
      await redisSubscriber.unsubscribe();
      await redisSubscriber.quit();
      log("Redis subscriber disconnected.");
    } catch (err) {
        log(`Error closing Redis connection: ${err}`);
    }
  }
  // ---------------------------------------------

  await browser.close();
  log('Bot execution completed.');
}
