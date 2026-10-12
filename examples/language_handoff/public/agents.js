export const agents = {
  en: { name: 'Alex', language: 'English', voice: 'us_male', locale: 'en' },
  es: { name: 'Sofía', language: 'Español', voice: 'mexican_female', locale: 'es-MX' },
  pt: { name: 'Clara', language: 'Português', voice: 'brazilian_female', locale: 'pt-BR' },
};

export const tools = [{
  name: 'transfer_language',
  description: 'Transfer to the assistant for the language the caller is speaking or explicitly requests. Use only when it differs from your own language.',
  parameters: {
    type: 'object',
    properties: { language: { type: 'string', enum: ['en', 'es', 'pt'] } },
    required: ['language'],
    additionalProperties: false,
  },
  read_only: false,
  requires_permission: false,
  recorded_bridge: 'never',
}];

export function agentConfig(language) {
  const agent = agents[language];
  if (!agent) throw new Error('Unsupported language. Choose English, Spanish or Portuguese.');
  return {
    voice: agent.voice,
    tools,
    instructions: `You are ${agent.name}, the ${agent.language}-speaking assistant in a language handoff demo.
Help the caller with simple everyday questions in ${agent.language}. Keep replies brief.
When the caller speaks another supported language or asks to switch, immediately call transfer_language with en (English), es (Spanish) or pt (Portuguese).
Do not translate the caller's request or answer it yourself before transferring. Do not transfer for a quoted phrase or a translation request.
After receiving a transferred conversation, briefly introduce yourself in your language and answer the caller's latest request using the existing conversation context.
If asked for an unsupported language, explain the three available languages. Never call transfer_language for your own language.`,
  };
}

// Keep the listening pipeline multilingual so handoffs can change voice language.
export const initialMode = {
  kind: 'dialt', ...agentConfig('en'), multilingual: true, greeting: false,
};

export async function transferLanguage(client, call, currentLanguage) {
  if (call.name !== 'transfer_language' || !Object.hasOwn(agents, call.args?.language)) {
    client.sendToolResult(call.id, { error: 'Unsupported transfer.' }, { outcome: 'failed' });
    return currentLanguage;
  }
  const language = call.args.language;
  if (language === currentLanguage) {
    client.sendToolResult(call.id, { already_active: true }, { outcome: 'succeeded', verified: true });
    return currentLanguage;
  }
  // One frame records the tool result, swaps instructions/tools/voice and starts
  // the incoming agent's reply. No close/reconnect and no separate reply race.
  const ack = await client.sendToolResult(call.id, { language }, {
    outcome: 'succeeded', verified: true,
    continueWith: {
      ...agentConfig(language),
      context: `The caller is now speaking or has requested ${agents[language].language}. Continue helping them.`,
    },
  });
  if (ack.status !== 'applied') {
    const reason = ack.detail || 'Transfer was not applied. Please try again.';
    // A rejected continuation leaves the original tool unresolved.
    client.sendToolResult(call.id, { error: reason }, { outcome: 'failed' });
    throw new Error(reason);
  }
  return language;
}
