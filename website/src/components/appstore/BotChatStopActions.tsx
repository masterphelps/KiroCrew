/**
 * The two ways out of a Bot-chat stop that did not finish, shared by the
 * uninstall, disable and trust-removal notices: stop the chat again, which keeps
 * its history, or restart the gateway.
 */
import { Link } from 'react-router-dom'

import { Btn } from '../ui'
import { i18nT } from '../../i18n/t'

export default function BotChatStopActions({
  bots,
  count,
  onRetry,
  retrying,
}: {
  /** The Bots' display names, joined, for the button label. */
  bots: string
  count: number
  onRetry: () => void
  retrying: boolean
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-3">
        <Btn disabled={retrying} onClick={onRetry}>
          {i18nT('pages.appsPage.bot_chat_stop_retry', { count, bots })}
        </Btn>
        <Link to="/settings/system" className="text-sm font-medium underline">
          {i18nT('pages.appsPage.restart_gateway_link')}
        </Link>
      </div>
      <span className="text-[12px] text-muted">{i18nT('pages.appsPage.bot_chat_stop_retry_hint')}</span>
    </div>
  )
}
