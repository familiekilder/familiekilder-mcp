(() => {
  'use strict';

  const SETUP_PORT = 28417;
  const LOCAL_ORIGIN = `http://127.0.0.1:${SETUP_PORT}`;
  const STATUS_PATH = '/status';
  const STATUS_URL = `${LOCAL_ORIGIN}${STATUS_PATH}`;
  const STATUS_APP = 'familiekilder-mcp';
  const SUPPORTED_PROTOCOLS = [1];

  const PERMISSION_NAMES = ['loopback-network', 'local-network-access'];
  const KNOWN_TIMEOUT_MS = 5000;
  const UNKNOWN_TIMEOUT_MS = 60000;
  const PROMPT_HELP_MS = 60000;

  const TEXT = {
    idleTitle: 'Sjekk om oppsettet kjører',
    idleDetail:
      'Nettleseren kan spørre om tilgang til apper på denne enheten når du starter sjekken. Beskjeden til agenten og direktelenken finnes under.',
    idleAction: 'Sjekk tilkoblingen',
    checkingTitle: 'Sjekker tilkoblingen …',
    checkingDetail:
      'Spør det lokale oppsettet på denne maskinen. Hvis nettleseren spør om tilgang til apper på enheten, velg Tillat.',
    checkingAction: 'Prøv igjen',
    contactTitle: 'Familiekilder kjører på denne maskinen',
    contactDetail: 'Veiviseren svarer lokalt. Åpne den i en ny fane for å fortsette oppsettet.',
    contactAction: 'Sjekk på nytt',
    noContactTitle: 'Vi får ikke kontakt med Familiekilder på denne maskinen',
    noContactDetail:
      'Du kan prøve igjen, åpne veiviseren direkte under, eller sende beskjeden under til AI-agenten.',
    noContactAction: 'Prøv igjen',
    copiedFeedback: 'Beskjeden er kopiert.',
    selectedFeedback: 'Beskjeden er merket. Kopier den med Ctrl+C eller Cmd+C.',
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function getPermissionsApi() {
    if (typeof navigator !== 'undefined' && navigator && navigator.permissions) {
      return navigator.permissions;
    }
    return null;
  }

  function getClipboardApi() {
    if (typeof navigator !== 'undefined' && navigator && navigator.clipboard) {
      return navigator.clipboard;
    }
    return null;
  }

  function getSelectionObject() {
    if (typeof getSelection === 'function') {
      return getSelection();
    }
    if (typeof window !== 'undefined' && window && typeof window.getSelection === 'function') {
      return window.getSelection();
    }
    return null;
  }

  async function queryPermission() {
    const permissions = getPermissionsApi();
    if (!permissions || typeof permissions.query !== 'function') {
      return null;
    }
    for (const name of PERMISSION_NAMES) {
      try {
        const status = await permissions.query({ name });
        if (status && typeof status.state === 'string') {
          return status;
        }
      } catch (_error) {
        // Browser does not recognize this permission name; try the next one.
      }
    }
    return null;
  }

  function isValidStatusPayload(data) {
    return (
      data !== null &&
      typeof data === 'object' &&
      !Array.isArray(data) &&
      data.app === STATUS_APP &&
      typeof data.protocol === 'number' &&
      SUPPORTED_PROTOCOLS.includes(data.protocol)
    );
  }

  let latestAttemptId = 0;
  let activeAttempt = null;

  function isCurrent(attempt) {
    return attempt.id === latestAttemptId;
  }

  function clearTimeoutSafe(timerId) {
    if (timerId !== null) {
      clearTimeout(timerId);
    }
  }

  function detachPermissionListener(attempt) {
    if (!attempt.permissionStatus || !attempt.permissionListener) {
      return;
    }
    if (typeof attempt.permissionStatus.removeEventListener === 'function') {
      attempt.permissionStatus.removeEventListener('change', attempt.permissionListener);
    } else if (attempt.permissionStatus.onchange === attempt.permissionListener) {
      attempt.permissionStatus.onchange = null;
    }
    attempt.permissionStatus = null;
    attempt.permissionListener = null;
  }

  function cleanupAttempt(attempt, abortRequest) {
    clearTimeoutSafe(attempt.timeoutId);
    attempt.timeoutId = null;
    clearTimeoutSafe(attempt.promptHelpId);
    attempt.promptHelpId = null;
    detachPermissionListener(attempt);
    if (abortRequest && !attempt.finished && attempt.controller) {
      attempt.controller.abort();
    }
    if (activeAttempt === attempt) {
      activeAttempt = null;
    }
  }

  function renderState(state, options = {}) {
    const panel = byId('check-panel');
    const title = byId('status-title');
    const detail = byId('status-detail');
    const promptHelp = byId('status-prompt-help');
    const denied = byId('status-denied');
    const continueLink = byId('continue-link');
    const checkButton = byId('check-button');
    const otherAddress = byId('other-address-note');

    panel.dataset.state = state;
    if (state === 'checking') {
      panel.setAttribute('aria-busy', 'true');
    } else {
      panel.removeAttribute('aria-busy');
    }

    promptHelp.hidden = !options.promptHelp;
    denied.hidden = !options.denied;
    continueLink.hidden = state !== 'contact';
    otherAddress.hidden = state !== 'contact';

    if (state === 'idle') {
      title.textContent = TEXT.idleTitle;
      detail.textContent = TEXT.idleDetail;
      checkButton.textContent = TEXT.idleAction;
      checkButton.className = 'primary';
    } else if (state === 'checking') {
      title.textContent = TEXT.checkingTitle;
      detail.textContent = TEXT.checkingDetail;
      checkButton.textContent = TEXT.checkingAction;
      checkButton.className = 'secondary';
    } else if (state === 'contact') {
      title.textContent = TEXT.contactTitle;
      detail.textContent = TEXT.contactDetail;
      checkButton.textContent = TEXT.contactAction;
      checkButton.className = 'secondary';
    } else if (state === 'no-contact') {
      title.textContent = TEXT.noContactTitle;
      detail.textContent = TEXT.noContactDetail;
      checkButton.textContent = TEXT.noContactAction;
      checkButton.className = 'primary';
    }
  }

  function scheduleAbortTimeout(attempt, delayMs) {
    clearTimeoutSafe(attempt.timeoutId);
    attempt.timeoutId = setTimeout(() => {
      attempt.timeoutId = null;
      if (!isCurrent(attempt) || attempt.finished) {
        return;
      }
      if (attempt.controller) {
        attempt.controller.abort();
      }
      handleFailure(attempt);
    }, delayMs);
  }

  async function handleFailure(attempt) {
    if (!isCurrent(attempt) || attempt.finished) {
      return;
    }
    attempt.finished = true;
    cleanupAttempt(attempt, false);
    const permissionStatus = await queryPermission();
    if (!isCurrent(attempt)) {
      return;
    }
    const isDenied = Boolean(permissionStatus && permissionStatus.state === 'denied');
    renderState('no-contact', { denied: isDenied });
  }

  function handleSuccess(attempt) {
    if (!isCurrent(attempt) || attempt.finished) {
      return;
    }
    attempt.finished = true;
    cleanupAttempt(attempt, false);
    renderState('contact');
  }

  function attachPermissionTimers(attempt, permissionStatus) {
    if (!isCurrent(attempt) || attempt.finished) {
      return;
    }
    if (!permissionStatus) {
      scheduleAbortTimeout(attempt, UNKNOWN_TIMEOUT_MS);
      return;
    }
    if (permissionStatus.state !== 'prompt') {
      scheduleAbortTimeout(attempt, KNOWN_TIMEOUT_MS);
      return;
    }

    // In prompt state, show neutral help after 60 s without aborting the pending request.
    attempt.promptHelpId = setTimeout(() => {
      attempt.promptHelpId = null;
      if (!isCurrent(attempt) || attempt.finished) {
        return;
      }
      byId('status-prompt-help').hidden = false;
    }, PROMPT_HELP_MS);

    const onChange = () => {
      if (!isCurrent(attempt) || attempt.finished) {
        return;
      }
      if (permissionStatus.state === 'prompt') {
        return;
      }
      detachPermissionListener(attempt);
      clearTimeoutSafe(attempt.promptHelpId);
      attempt.promptHelpId = null;
      byId('status-prompt-help').hidden = true;
      scheduleAbortTimeout(attempt, KNOWN_TIMEOUT_MS);
    };

    attempt.permissionStatus = permissionStatus;
    attempt.permissionListener = onChange;
    if (typeof permissionStatus.addEventListener === 'function') {
      permissionStatus.addEventListener('change', onChange);
    } else {
      permissionStatus.onchange = onChange;
    }
  }

  function startCheck() {
    if (activeAttempt) {
      cleanupAttempt(activeAttempt, true);
      activeAttempt = null;
    }

    latestAttemptId += 1;
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const attempt = {
      id: latestAttemptId,
      controller,
      timeoutId: null,
      promptHelpId: null,
      permissionStatus: null,
      permissionListener: null,
      finished: false,
    };
    activeAttempt = attempt;

    renderState('checking');

    // Start the loopback request synchronously inside the click handler before querying permission.
    const requestOptions = {
      method: 'GET',
      cache: 'no-store',
      credentials: 'omit',
      redirect: 'error',
    };
    if (controller) {
      requestOptions.signal = controller.signal;
    }

    let fetchPromise;
    try {
      fetchPromise = fetch(STATUS_URL, requestOptions);
    } catch (_error) {
      handleFailure(attempt);
      return;
    }

    queryPermission().then((permissionStatus) => {
      attachPermissionTimers(attempt, permissionStatus);
    });

    Promise.resolve(fetchPromise).then(
      async (response) => {
        if (!isCurrent(attempt) || attempt.finished) {
          return;
        }
        if (!response || !response.ok) {
          await handleFailure(attempt);
          return;
        }
        let payload;
        try {
          payload = await response.json();
        } catch (_error) {
          await handleFailure(attempt);
          return;
        }
        if (!isCurrent(attempt) || attempt.finished) {
          return;
        }
        if (!isValidStatusPayload(payload)) {
          await handleFailure(attempt);
          return;
        }
        handleSuccess(attempt);
      },
      () => {
        handleFailure(attempt);
      },
    );
  }

  function selectNodeContents(node) {
    if (typeof document.createRange !== 'function') {
      return;
    }
    const selection = getSelectionObject();
    if (!selection) {
      return;
    }
    const range = document.createRange();
    range.selectNodeContents(node);
    selection.removeAllRanges();
    selection.addRange(range);
  }

  async function copyAgentMessage() {
    const messageNode = byId('agent-message');
    const feedbackNode = byId('copy-feedback');
    const text = messageNode.textContent;
    const clipboard = getClipboardApi();
    try {
      if (!clipboard || typeof clipboard.writeText !== 'function') {
        throw new Error('Clipboard API is unavailable');
      }
      await clipboard.writeText(text);
      feedbackNode.textContent = TEXT.copiedFeedback;
      feedbackNode.hidden = false;
    } catch (_error) {
      // Without clipboard access, select the message so the user can copy it manually.
      selectNodeContents(messageNode);
      feedbackNode.textContent = TEXT.selectedFeedback;
      feedbackNode.hidden = false;
    }
  }

  function init() {
    byId('check-panel').hidden = false;
    byId('copy-message').hidden = false;
    renderState('idle');
    byId('check-button').addEventListener('click', startCheck);
    byId('copy-message').addEventListener('click', copyAgentMessage);
  }

  if (document.readyState === 'loading' && typeof document.addEventListener === 'function') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
