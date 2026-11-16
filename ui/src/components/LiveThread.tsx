import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Client } from '@langchain/langgraph-sdk';
import { AssistantRuntimeProvider, ThreadPrimitive, MessagePrimitive, useAuiState, type RemoteThreadListAdapter, type ToolCallMessagePartProps } from '@assistant-ui/react';
import { MarkdownTextPrimitive } from '@assistant-ui/react-markdown';
import { useLangGraphRuntime, useLangGraphSend, type LangChainMessage, type LangGraphStreamCallback } from '@assistant-ui/react-langgraph';
import { useI18n } from '../i18n';
import type { Run } from '../types';
import Icon from './Icon';

function ToolPart({ toolName, args, result }: ToolCallMessagePartProps) {
  const { t } = useI18n();
  return (
    <details className="agent-tool-event">
      <summary>{t('thread.tool', { name: toolName })}</summary>
      <pre>{JSON.stringify(args, null, 2)}</pre>
      {result !== undefined && <pre>{typeof result === 'string' ? result : JSON.stringify(result, null, 2)}</pre>}
    </details>
  );
}

function Markdown() {
  const { t } = useI18n();
  // Raw HTML is skipped, images are never loaded automatically, external links open in a new, isolated window.
  return (
    <MarkdownTextPrimitive
      className="studio-markdown"
      smooth={false}
      skipHtml
      components={{
        a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
        img: ({ alt }) => <span className="studio-omitted-image">[{t('thread.image', { alt: alt || t('thread.imageNotLoaded') })}]</span>,
      }}
    />
  );
}

function Message() {
  const { t } = useI18n();
  const role = useAuiState((state) => state.message.role);
  return (
    <MessagePrimitive.Root className={'agent-message agent-message-' + role}>
      <div className="studio-message-author">
        <span><Icon name={role === 'user' ? 'file' : 'spark'} size={15} /></span>
        <strong>{role === 'user' ? t('thread.task') : t('thread.agent')}</strong>
      </div>
      <MessagePrimitive.Parts components={{ Text: Markdown, tools: { Fallback: ToolPart } }} />
      <MessagePrimitive.Error />
    </MessagePrimitive.Root>
  );
}

/** Joins the run's live stream once the saved state is loaded. It never starts a run: that happens in the composer. */
function LiveControls({ active, loaded, onSettled }: { active: boolean; loaded: boolean; onSettled: () => void }) {
  const { t } = useI18n();
  const send = useLangGraphSend();
  const loading = useAuiState((state) => state.thread.isLoading);
  const joined = useRef(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!loaded || loading || joined.current || !active) return;
    joined.current = true;
    void send([], {}).then(onSettled).catch(() => setError(t('thread.liveLost')));
  }, [loaded, loading, active, send, onSettled, t]);
  return (
    <>
      {loading && <p>{t('thread.loading')}</p>}
      {error && <p role="alert">{error}</p>}
    </>
  );
}

export default function LiveThread({ run, onSettled }: { run: Run; onSettled: () => void }) {
  const { t } = useI18n();
  const client = useMemo(() => new Client({ apiUrl: new URL('/api/langgraph', window.location.href).href }), []);
  const [loaded, setLoaded] = useState(false);
  const [subgraphs, setSubgraphs] = useState(0);
  const [failure, setFailure] = useState('');
  const cursor = useRef('0');

  const load = useCallback(
    async (id: string) => {
      const state = await client.threads.getState<{ messages: LangChainMessage[] }>(id);
      cursor.current = String((state.metadata as { stream_cursor?: number } | undefined)?.stream_cursor ?? 0);
      setLoaded(true);
      return { messages: state.values.messages, interrupts: state.tasks.flatMap((task) => task.interrupts || []) };
    },
    [client],
  );

  const stream = useCallback<LangGraphStreamCallback<LangChainMessage>>(
    async function* (messages, config) {
      if (messages.length) throw new Error(t('thread.newTaskInComposer'));
      const { externalId } = await config.initialize();
      if (externalId !== run.id) throw new Error('Thread id mismatch.');
      const events = client.runs.joinStream(run.id, run.id, { lastEventId: cursor.current, signal: config.abortSignal });
      for await (const event of events) yield event;
    },
    [client, run.id, t],
  );

  const adapter = useMemo<RemoteThreadListAdapter>(() => {
    const thread = { remoteId: run.id, externalId: run.id, status: 'regular' as const, title: run.prompt.slice(0, 90) };
    const unsupported = async () => {
      throw new Error(t('thread.unsupported'));
    };
    return {
      list: async () => ({ threads: [thread] }),
      fetch: async (id: string) => {
        if (id !== run.id) throw new Error('Not found.');
        return thread;
      },
      initialize: async () => ({ remoteId: run.id, externalId: run.id }),
      rename: unsupported, archive: unsupported, delete: unsupported, generateTitle: unsupported,
    } as unknown as RemoteThreadListAdapter;
  }, [run.id, run.prompt, t]);

  const runtime = useLangGraphRuntime({
    threadId: run.id,
    stream,
    load,
    unstable_threadListAdapter: adapter,
    autoCancelPendingToolCalls: false,
    eventHandlers: {
      onSubgraphValues: () => setSubgraphs((count) => count + 1),
      onSubgraphUpdates: () => setSubgraphs((count) => count + 1),
      onError: () => setFailure(t('thread.failure')),
    },
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="agent-live" data-agent-runtime="assistant-ui-langgraph" data-thread-id={run.id}>
        <div className="studio-transcript-meta">
          <span><Icon name="message" size={13} />assistant-ui / LangGraph SDK</span>
          <span>{t('thread.subgraphEvents', { count: subgraphs })}</span>
        </div>
        {failure && <p role="alert">{failure}</p>}
        <ThreadPrimitive.Root>
          <ThreadPrimitive.Viewport className="agent-transcript">
            <ThreadPrimitive.Messages components={{ Message }} />
          </ThreadPrimitive.Viewport>
        </ThreadPrimitive.Root>
        <LiveControls active={run.status === 'running'} loaded={loaded} onSettled={onSettled} />
      </div>
    </AssistantRuntimeProvider>
  );
}
