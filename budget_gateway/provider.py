import os
import httpx


class Provider:
    async def generate(self, model, body):
        base = os.getenv('MODEL_BASE_URL')
        if not base:
            text = '[DEMO: no model called] '+('Detailed response' if model=='large' else 'Concise response')+': '+body.messages[-1].content[:180]
            # Keep demo output within an approximate whitespace-token ceiling.
            text = ' '.join(text.split()[:body.max_tokens])
            return text, {'prompt_tokens':sum(len(m.content.split()) for m in body.messages),'completion_tokens':len(text.split())}, 'demo'
        async with httpx.AsyncClient(timeout=float(os.getenv('PROVIDER_TIMEOUT_SECONDS','30'))) as client:
            response = await client.post(base.rstrip('/')+'/chat/completions',
                headers={'Authorization':'Bearer '+os.getenv('MODEL_API_KEY','local')},
                json={'model':os.getenv(model.upper()+'_MODEL',model), 'messages':[m.model_dump() for m in body.messages],
                      'max_tokens':body.max_tokens,'temperature':body.temperature,'stream':False})
            response.raise_for_status()
            data = response.json()
            text = data['choices'][0]['message']['content']
            usage = data['usage']
            if not isinstance(text,str) or not all(type(usage[k]) is int and usage[k]>=0 for k in ('prompt_tokens','completion_tokens')):
                raise ValueError('Provider returned invalid content or usage')
            return text, usage, 'live'
