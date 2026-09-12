const en = {
    pageTitle: 'Webstation',
    localUsername: 'You',
    noSession: {
        title: 'No Active Session',
        message: 'There is no game session running, or this link is no longer valid.',
    },
    settings: {
        title: 'Settings',
        microphoneLabel: 'Microphone',
        webcamLabel: 'Webcam',
    },
    alerts: {
        mediaAccessError: 'Could not access your camera or microphone: {message}',
    },
    devices: {
        unlabeledDevice: '{kind} device {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'Mute/Unmute Audio',
        toggleRemoteVideo: 'Mute/Unmute Video',
        toggleLocalMic: 'Mute/Unmute Microphone',
        toggleLocalWebcam: 'Start/Stop Webcam',
        toggleSessionAudio: 'Mute/Unmute Session Audio',
        sessionVolume: 'Session Volume',
        reply: 'Reply',
        cancelReply: 'Cancel Reply',
        designateSpeaker: 'Designate as Speaker',
        reloadStream: 'Reload Stream',
        gamingMode: 'Gaming Mode',
        lockResolution: 'Lock/Unlock Resolution',
        resizeClient: 'Resize to Client',
        invite: 'Invite someone to this session',
    },
    usernamePrompt: {
        title: 'Welcome!',
        description: 'Please choose a username to join the session.',
        placeholder: 'Your Name',
        joinButton: 'Join',
    },
    chat: {
        inputPlaceholder: 'Type a message...',
        selfUsername: 'You',
        replyingTo: 'Replying to <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> has joined the room.',
        userLeft: '<b>{username}</b> has left the room.',
        usernameChanged: '<b>{old_username}</b> is now known as <b>{new_username}</b>.',
    },
    inviteLinks: {
        participant: 'Player Invite Link',
        readonly: 'Viewer Invite Link',
        copied: 'Link copied',
        failed: 'Could not create an invite link',
    },
    disconnect: {
        title: 'Disconnected',
        message: 'The session has ended.',
    },
    waiting: {
        title: 'Controller is Away',
        message: 'The session is active. Waiting for the controller to resume the stream.',
    },
};

// Spanish
const es = {
    pageTitle: 'Colaboración Webstation',
    localUsername: 'Tú',
    settings: {
        title: 'Ajustes',
        microphoneLabel: 'Micrófono',
        webcamLabel: 'Cámara web',
    },
    alerts: {
        mediaAccessError: 'No se pudo acceder a tu cámara o micrófono: {message}',
    },
    devices: {
        unlabeledDevice: 'Dispositivo {kind} {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'Silenciar/Activar audio',
        toggleRemoteVideo: 'Silenciar/Activar video',
        toggleLocalMic: 'Silenciar/Activar micrófono',
        toggleLocalWebcam: 'Iniciar/Detener cámara web',
        toggleSessionAudio: 'Silenciar/Activar audio de la sesión',
        sessionVolume: 'Volumen de la sesión',
        reply: 'Responder',
        cancelReply: 'Cancelar respuesta',
        designateSpeaker: 'Designar como Orador',
        reloadStream: 'Recargar transmisión',
        gamingMode: 'Modo Juego',
        lockResolution: 'Bloquear/Desbloquear resolución',
        resizeClient: 'Redimensionar al cliente',
        invite: 'Invitar a alguien a esta sesión',
    },
    usernamePrompt: {
        title: '¡Bienvenido!',
        description: 'Por favor, elige un nombre de usuario para unirte a la sesión.',
        placeholder: 'Tu nombre',
        joinButton: 'Unirse',
    },
    chat: {
        inputPlaceholder: 'Escribe un mensaje...',
        selfUsername: 'Tú',
        replyingTo: 'Respondiendo a <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> se ha unido a la sala.',
        userLeft: '<b>{username}</b> ha abandonado la sala.',
        usernameChanged: '<b>{old_username}</b> ahora es conocido como <b>{new_username}</b>.',
    },
    inviteLinks: {
        participant: 'Enlace de invitación de jugador',
        readonly: 'Enlace de invitación de espectador',
        copied: 'Enlace copiado',
        failed: 'No se pudo crear un enlace de invitación',
    },
    disconnect: {
        title: 'Desconectado',
        message: 'La sesión ha finalizado.',
    },
    waiting: {
        title: 'El controlador está ausente',
        message: 'La sesión está activa. Esperando a que el controlador reanude la transmisión.',
    },
};

// Chinese (Simplified)
const zh = {
    pageTitle: 'Webstation 协作',
    localUsername: '您',
    settings: {
        title: '设置',
        microphoneLabel: '麦克风',
        webcamLabel: '网络摄像头',
    },
    alerts: {
        mediaAccessError: '无法访问您的摄像头或麦克风：{message}',
    },
    devices: {
        unlabeledDevice: '{kind} 设备 {number}',
    },
    tooltips: {
        toggleRemoteAudio: '静音/取消静音音频',
        toggleRemoteVideo: '静音/取消静音视频',
        toggleLocalMic: '静音/取消静音麦克风',
        toggleLocalWebcam: '启动/停止网络摄像头',
        toggleSessionAudio: '静音/取消静音会话音频',
        sessionVolume: '会话音量',
        reply: '回复',
        cancelReply: '取消回复',
        designateSpeaker: '指定为发言人',
        reloadStream: '重新加载流',
        gamingMode: '游戏模式',
        lockResolution: '锁定/解锁分辨率',
        resizeClient: '调整为客户端大小',
        invite: '邀请他人加入此会话',
    },
    usernamePrompt: {
        title: '欢迎！',
        description: '请选择一个用户名以加入会话。',
        placeholder: '您的名字',
        joinButton: '加入',
    },
    chat: {
        inputPlaceholder: '输入消息...',
        selfUsername: '您',
        replyingTo: '回复 <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> 已加入房间。',
        userLeft: '<b>{username}</b> 已离开房间。',
        usernameChanged: '<b>{old_username}</b> 现已更名为 <b>{new_username}</b>。',
    },
    inviteLinks: {
        participant: '玩家邀请链接',
        readonly: '观众邀请链接',
        copied: '链接已复制',
        failed: '无法创建邀请链接',
    },
    disconnect: {
        title: '已断开连接',
        message: '会话已结束。',
    },
    waiting: {
        title: '控制者已离开',
        message: '会话处于活动状态。正在等待控制者恢复流。',
    },
};

// Hindi
const hi = {
    pageTitle: 'Webstation सहयोग',
    localUsername: 'आप',
    settings: {
        title: 'सेटिंग्स',
        microphoneLabel: 'माइक्रोफ़ोन',
        webcamLabel: 'वेबकैम',
    },
    alerts: {
        mediaAccessError: 'आपके कैमरे या माइक्रोफ़ोन तक नहीं पहुँच सका: {message}',
    },
    devices: {
        unlabeledDevice: '{kind} डिवाइस {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'ऑडियो म्यूट/अनम्यूट करें',
        toggleRemoteVideo: 'वीडियो म्यूट/अनम्यूट करें',
        toggleLocalMic: 'माइक्रोफ़ोन म्यूट/अनम्यूट करें',
        toggleLocalWebcam: 'वेबकैम शुरू/बंद करें',
        toggleSessionAudio: 'सत्र ऑडियो म्यूट/अनम्यूट करें',
        sessionVolume: 'सत्र वॉल्यूम',
        reply: 'उत्तर दें',
        cancelReply: 'उत्तर रद्द करें',
        designateSpeaker: 'वक्ता के रूप में नामित करें',
        reloadStream: 'स्ट्रीम पुनः लोड करें',
        gamingMode: 'गेमिंग मोड',
        lockResolution: 'रिज़ॉल्यूशन लॉक/अनलॉक करें',
        resizeClient: 'क्लाइंट के आकार में बदलें',
        invite: 'किसी को इस सत्र में आमंत्रित करें',
    },
    usernamePrompt: {
        title: 'स्वागत है!',
        description: 'सत्र में शामिल होने के लिए कृपया एक उपयोगकर्ता नाम चुनें।',
        placeholder: 'आपका नाम',
        joinButton: 'शामिल हों',
    },
    chat: {
        inputPlaceholder: 'एक संदेश लिखें...',
        selfUsername: 'आप',
        replyingTo: '<b>{sender}</b> को उत्तर दे रहे हैं',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> कमरे में शामिल हो गए हैं।',
        userLeft: '<b>{username}</b> ने कमरा छोड़ दिया है।',
        usernameChanged: '<b>{old_username}</b> को अब <b>{new_username}</b> के नाम से जाना जाता है।',
    },
    inviteLinks: {
        participant: 'खिलाड़ी आमंत्रण लिंक',
        readonly: 'दर्शक आमंत्रण लिंक',
        copied: 'लिंक कॉपी हो गया',
        failed: 'आमंत्रण लिंक नहीं बनाया जा सका',
    },
    disconnect: {
        title: 'डिस्कनेक्ट हो गया',
        message: 'सत्र समाप्त हो गया है।',
    },
    waiting: {
        title: 'नियंत्रक अनुपस्थित है',
        message: 'सत्र सक्रिय है। नियंत्रक द्वारा स्ट्रीम फिर से शुरू करने की प्रतीक्षा की जा रही है।',
    },
};

// Portuguese
const pt = {
    pageTitle: 'Colaboração Webstation',
    localUsername: 'Você',
    settings: {
        title: 'Configurações',
        microphoneLabel: 'Microfone',
        webcamLabel: 'Webcam',
    },
    alerts: {
        mediaAccessError: 'Não foi possível acessar sua câmera ou microfone: {message}',
    },
    devices: {
        unlabeledDevice: 'Dispositivo {kind} {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'Ativar/Desativar áudio',
        toggleRemoteVideo: 'Ativar/Desativar vídeo',
        toggleLocalMic: 'Ativar/Desativar microfone',
        toggleLocalWebcam: 'Iniciar/Parar webcam',
        toggleSessionAudio: 'Ativar/Desativar áudio da sessão',
        sessionVolume: 'Volume da sessão',
        reply: 'Responder',
        cancelReply: 'Cancelar resposta',
        designateSpeaker: 'Designar como Orador',
        reloadStream: 'Recarregar Transmissão',
        gamingMode: 'Modo Jogo',
        lockResolution: 'Bloquear/Desbloquear Resolução',
        resizeClient: 'Redimensionar para o Cliente',
        invite: 'Convidar alguém para esta sessão',
    },
    usernamePrompt: {
        title: 'Bem-vindo(a)!',
        description: 'Por favor, escolha um nome de usuário para entrar na sessão.',
        placeholder: 'Seu nome',
        joinButton: 'Entrar',
    },
    chat: {
        inputPlaceholder: 'Digite uma mensagem...',
        selfUsername: 'Você',
        replyingTo: 'Respondendo a <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> entrou na sala.',
        userLeft: '<b>{username}</b> saiu da sala.',
        usernameChanged: '<b>{old_username}</b> agora é conhecido(a) como <b>{new_username}</b>.',
    },
    inviteLinks: {
        participant: 'Link de convite de jogador',
        readonly: 'Link de convite de espectador',
        copied: 'Link copiado',
        failed: 'Não foi possível criar um link de convite',
    },
    disconnect: {
        title: 'Desconectado',
        message: 'A sessão terminou.',
    },
    waiting: {
        title: 'O controlador está ausente',
        message: 'A sessão está ativa. Aguardando o controlador retomar a transmissão.',
    },
};

// French
const fr = {
    pageTitle: 'Collaboration Webstation',
    localUsername: 'Vous',
    settings: {
        title: 'Paramètres',
        microphoneLabel: 'Microphone',
        webcamLabel: 'Webcam',
    },
    alerts: {
        mediaAccessError: 'Impossible d\'accéder à votre caméra ou à votre microphone : {message}',
    },
    devices: {
        unlabeledDevice: 'Appareil {kind} {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'Activer/Désactiver l\'audio',
        toggleRemoteVideo: 'Activer/Désactiver la vidéo',
        toggleLocalMic: 'Activer/Désactiver le microphone',
        toggleLocalWebcam: 'Démarrer/Arrêter la webcam',
        toggleSessionAudio: 'Activer/Désactiver l\'audio de la session',
        sessionVolume: 'Volume de la session',
        reply: 'Répondre',
        cancelReply: 'Annuler la réponse',
        designateSpeaker: 'Désigner comme Orateur',
        reloadStream: 'Recharger le flux',
        gamingMode: 'Mode Jeu',
        lockResolution: 'Verrouiller/Déverrouiller la résolution',
        resizeClient: 'Redimensionner au client',
        invite: 'Inviter quelqu\'un à cette session',
    },
    usernamePrompt: {
        title: 'Bienvenue !',
        description: 'Veuillez choisir un nom d\'utilisateur pour rejoindre la session.',
        placeholder: 'Votre nom',
        joinButton: 'Rejoindre',
    },
    chat: {
        inputPlaceholder: 'Saisissez un message...',
        selfUsername: 'Vous',
        replyingTo: 'En réponse à <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> a rejoint la salle.',
        userLeft: '<b>{username}</b> a quitté la salle.',
        usernameChanged: '<b>{old_username}</b> est maintenant connu(e) sous le nom de <b>{new_username}</b>.',
    },
    inviteLinks: {
        participant: 'Lien d\'invitation joueur',
        readonly: 'Lien d\'invitation spectateur',
        copied: 'Lien copié',
        failed: 'Impossible de créer un lien d\'invitation',
    },
    disconnect: {
        title: 'Déconnecté',
        message: 'La session est terminée.',
    },
    waiting: {
        title: 'Le contrôleur est absent',
        message: 'La session est active. En attente de la reprise du flux par le contrôleur.',
    },
};

// Russian
const ru = {
    pageTitle: 'Совместная работа Webstation',
    localUsername: 'Вы',
    settings: {
        title: 'Настройки',
        microphoneLabel: 'Микрофон',
        webcamLabel: 'Веб-камера',
    },
    alerts: {
        mediaAccessError: 'Не удалось получить доступ к вашей камере или микрофону: {message}',
    },
    devices: {
        unlabeledDevice: 'Устройство {kind} {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'Включить/выключить звук',
        toggleRemoteVideo: 'Включить/выключить видео',
        toggleLocalMic: 'Включить/выключить микрофон',
        toggleLocalWebcam: 'Запустить/остановить веб-камеру',
        toggleSessionAudio: 'Включить/выключить звук сеанса',
        sessionVolume: 'Громкость сеанса',
        reply: 'Ответить',
        cancelReply: 'Отменить ответ',
        designateSpeaker: 'Назначить докладчиком',
        reloadStream: 'Перезагрузить поток',
        gamingMode: 'Игровой режим',
        lockResolution: 'Заблокировать/Разблокировать разрешение',
        resizeClient: 'Изменить размер под клиента',
        invite: 'Пригласить кого-то в этот сеанс',
    },
    usernamePrompt: {
        title: 'Добро пожаловать!',
        description: 'Пожалуйста, выберите имя пользователя, чтобы присоединиться к сеансу.',
        placeholder: 'Ваше имя',
        joinButton: 'Присоединиться',
    },
    chat: {
        inputPlaceholder: 'Введите сообщение...',
        selfUsername: 'Вы',
        replyingTo: 'Ответ пользователю <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> присоединился(ась) к комнате.',
        userLeft: '<b>{username}</b> покинул(а) комнату.',
        usernameChanged: '<b>{old_username}</b> теперь известен(на) как <b>{new_username}</b>.',
    },
    inviteLinks: {
        participant: 'Ссылка-приглашение для игрока',
        readonly: 'Ссылка-приглашение для зрителя',
        copied: 'Ссылка скопирована',
        failed: 'Не удалось создать ссылку-приглашение',
    },
    disconnect: {
        title: 'Отключено',
        message: 'Сеанс завершен.',
    },
    waiting: {
        title: 'Контроллер отошел',
        message: 'Сеанс активен. Ожидание возобновления трансляции контроллером.',
    },
};

// German
const de = {
    pageTitle: 'Webstation Kollaboration',
    localUsername: 'Sie',
    settings: {
        title: 'Einstellungen',
        microphoneLabel: 'Mikrofon',
        webcamLabel: 'Webcam',
    },
    alerts: {
        mediaAccessError: 'Zugriff auf Ihre Kamera oder Ihr Mikrofon fehlgeschlagen: {message}',
    },
    devices: {
        unlabeledDevice: '{kind}-Gerät {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'Audio stummschalten/aktivieren',
        toggleRemoteVideo: 'Video stummschalten/aktivieren',
        toggleLocalMic: 'Mikrofon stummschalten/aktivieren',
        toggleLocalWebcam: 'Webcam starten/stoppen',
        toggleSessionAudio: 'Sitzungs-Audio stummschalten/aktivieren',
        sessionVolume: 'Sitzungslautstärke',
        reply: 'Antworten',
        cancelReply: 'Antwort abbrechen',
        designateSpeaker: 'Als Sprecher festlegen',
        reloadStream: 'Stream neu laden',
        gamingMode: 'Gaming-Modus',
        lockResolution: 'Auflösung sperren/entsperren',
        resizeClient: 'Größe an Client anpassen',
        invite: 'Jemanden zu dieser Sitzung einladen',
    },
    usernamePrompt: {
        title: 'Willkommen!',
        description: 'Bitte wählen Sie einen Benutzernamen, um der Sitzung beizutreten.',
        placeholder: 'Ihr Name',
        joinButton: 'Beitreten',
    },
    chat: {
        inputPlaceholder: 'Nachricht eingeben...',
        selfUsername: 'Sie',
        replyingTo: 'Antwort an <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> ist dem Raum beigetreten.',
        userLeft: '<b>{username}</b> hat den Raum verlassen.',
        usernameChanged: '<b>{old_username}</b> ist jetzt als <b>{new_username}</b> bekannt.',
    },
    inviteLinks: {
        participant: 'Spieler-Einladungslink',
        readonly: 'Zuschauer-Einladungslink',
        copied: 'Link kopiert',
        failed: 'Einladungslink konnte nicht erstellt werden',
    },
    disconnect: {
        title: 'Verbindung getrennt',
        message: 'Die Sitzung wurde beendet.',
    },
    waiting: {
        title: 'Controller ist abwesend',
        message: 'Die Sitzung ist aktiv. Warten auf Wiederaufnahme des Streams durch den Controller.',
    },
};

// Turkish
const tr = {
    pageTitle: 'Webstation İşbirliği',
    localUsername: 'Siz',
    settings: {
        title: 'Ayarlar',
        microphoneLabel: 'Mikrofon',
        webcamLabel: 'Web Kamerası',
    },
    alerts: {
        mediaAccessError: 'Kameranıza veya mikrofonunuza erişilemedi: {message}',
    },
    devices: {
        unlabeledDevice: '{kind} cihazı {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'Sesi Aç/Kapat',
        toggleRemoteVideo: 'Videoyu Aç/Kapat',
        toggleLocalMic: 'Mikrofonu Aç/Kapat',
        toggleLocalWebcam: 'Web Kamerasını Başlat/Durdur',
        toggleSessionAudio: 'Oturum Sesini Aç/Kapat',
        sessionVolume: 'Oturum Sesi',
        reply: 'Yanıtla',
        cancelReply: 'Yanıtı İptal Et',
        designateSpeaker: 'Konuşmacı Olarak Belirle',
        reloadStream: 'Yayını Yenile',
        gamingMode: 'Oyun Modu',
        lockResolution: 'Çözünürlüğü Kilitle/Kilidini Aç',
        resizeClient: 'İstemciye Göre Yeniden Boyutlandır',
        invite: 'Birini bu oturuma davet et',
    },
    usernamePrompt: {
        title: 'Hoş geldiniz!',
        description: 'Oturuma katılmak için lütfen bir kullanıcı adı seçin.',
        placeholder: 'Adınız',
        joinButton: 'Katıl',
    },
    chat: {
        inputPlaceholder: 'Bir mesaj yazın...',
        selfUsername: 'Siz',
        replyingTo: '<b>{sender}</b> adlı kişiye yanıt veriliyor',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> odaya katıldı.',
        userLeft: '<b>{username}</b> odadan ayrıldı.',
        usernameChanged: '<b>{old_username}</b> artık <b>{new_username}</b> olarak biliniyor.',
    },
    inviteLinks: {
        participant: 'Oyuncu Davet Bağlantısı',
        readonly: 'İzleyici Davet Bağlantısı',
        copied: 'Bağlantı kopyalandı',
        failed: 'Davet bağlantısı oluşturulamadı',
    },
    disconnect: {
        title: 'Bağlantı Kesildi',
        message: 'Oturum sona erdi.',
    },
    waiting: {
        title: 'Kontrolcü Uzakta',
        message: 'Oturum aktif. Kontrolcünün yayını sürdürmesi bekleniyor.',
    },
};

// Italian
const it = {
    pageTitle: 'Collaborazione Webstation',
    localUsername: 'Tu',
    settings: {
        title: 'Impostazioni',
        microphoneLabel: 'Microfono',
        webcamLabel: 'Webcam',
    },
    alerts: {
        mediaAccessError: 'Impossibile accedere alla tua fotocamera o al tuo microfono: {message}',
    },
    devices: {
        unlabeledDevice: 'Dispositivo {kind} {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'Attiva/Disattiva audio',
        toggleRemoteVideo: 'Attiva/Disattiva video',
        toggleLocalMic: 'Attiva/Disattiva microfono',
        toggleLocalWebcam: 'Avvia/Interrompi webcam',
        toggleSessionAudio: 'Attiva/Disattiva audio della sessione',
        sessionVolume: 'Volume della sessione',
        reply: 'Rispondi',
        cancelReply: 'Annulla risposta',
        designateSpeaker: 'Designa come Relatore',
        reloadStream: 'Ricarica Stream',
        gamingMode: 'Modalità Gioco',
        lockResolution: 'Blocca/Sblocca risoluzione',
        resizeClient: 'Ridimensiona al client',
        invite: 'Invita qualcuno a questa sessione',
    },
    usernamePrompt: {
        title: 'Benvenuto!',
        description: 'Scegli un nome utente per partecipare alla sessione.',
        placeholder: 'Il tuo nome',
        joinButton: 'Partecipa',
    },
    chat: {
        inputPlaceholder: 'Scrivi un messaggio...',
        selfUsername: 'Tu',
        replyingTo: 'In risposta a <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> è entrato/a nella stanza.',
        userLeft: '<b>{username}</b> ha lasciato la stanza.',
        usernameChanged: '<b>{old_username}</b> è ora conosciuto/a come <b>{new_username}</b>.',
    },
    inviteLinks: {
        participant: 'Link di invito giocatore',
        readonly: 'Link di invito spettatore',
        copied: 'Link copiato',
        failed: 'Impossibile creare un link di invito',
    },
    disconnect: {
        title: 'Disconnesso',
        message: 'La sessione è terminata.',
    },
    waiting: {
        title: 'Il controllore è assente',
        message: 'La sessione è attiva. In attesa che il controllore riprenda lo stream.',
    },
};

// Dutch
const nl = {
    pageTitle: 'Webstation Samenwerking',
    localUsername: 'Jij',
    settings: {
        title: 'Instellingen',
        microphoneLabel: 'Microfoon',
        webcamLabel: 'Webcam',
    },
    alerts: {
        mediaAccessError: 'Kon geen toegang krijgen tot uw camera of microfoon: {message}',
    },
    devices: {
        unlabeledDevice: '{kind}-apparaat {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'Audio dempen/dempen opheffen',
        toggleRemoteVideo: 'Video dempen/dempen opheffen',
        toggleLocalMic: 'Microfoon dempen/dempen opheffen',
        toggleLocalWebcam: 'Webcam starten/stoppen',
        toggleSessionAudio: 'Sessieaudio dempen/dempen opheffen',
        sessionVolume: 'Sessievolume',
        reply: 'Beantwoorden',
        cancelReply: 'Antwoord annuleren',
        designateSpeaker: 'Aanwijzen als spreker',
        reloadStream: 'Stream herladen',
        gamingMode: 'Gamingmodus',
        lockResolution: 'Resolutie vergrendelen/ontgrendelen',
        resizeClient: 'Formaat aanpassen aan client',
        invite: 'Nodig iemand uit voor deze sessie',
    },
    usernamePrompt: {
        title: 'Welkom!',
        description: 'Kies een gebruikersnaam om deel te nemen aan de sessie.',
        placeholder: 'Jouw naam',
        joinButton: 'Deelnemen',
    },
    chat: {
        inputPlaceholder: 'Typ een bericht...',
        selfUsername: 'Jij',
        replyingTo: 'Antwoord op <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> is de kamer binnengekomen.',
        userLeft: '<b>{username}</b> heeft de kamer verlaten.',
        usernameChanged: '<b>{old_username}</b> is nu bekend als <b>{new_username}</b>.',
    },
    inviteLinks: {
        participant: 'Uitnodigingslink voor speler',
        readonly: 'Uitnodigingslink voor kijker',
        copied: 'Link gekopieerd',
        failed: 'Kon geen uitnodigingslink maken',
    },
    disconnect: {
        title: 'Verbinding verbroken',
        message: 'De sessie is beëindigd.',
    },
    waiting: {
        title: 'Controller is afwezig',
        message: 'De sessie is actief. Wachten tot de controller de stream hervat.',
    },
};

// Arabic
const ar = {
    pageTitle: 'تعاون Webstation',
    localUsername: 'أنت',
    settings: {
        title: 'الإعدادات',
        microphoneLabel: 'الميكروفون',
        webcamLabel: 'كاميرا الويب',
    },
    alerts: {
        mediaAccessError: 'تعذر الوصول إلى الكاميرا أو الميكروفون: {message}',
    },
    devices: {
        unlabeledDevice: 'جهاز {kind} {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'كتم/إلغاء كتم الصوت',
        toggleRemoteVideo: 'كتم/إلغاء كتم الفيديو',
        toggleLocalMic: 'كتم/إلغاء كتم الميكروفون',
        toggleLocalWebcam: 'بدء/إيقاف كاميرا الويب',
        toggleSessionAudio: 'كتم/إلغاء كتم صوت الجلسة',
        sessionVolume: 'مستوى صوت الجلسة',
        reply: 'رد',
        cancelReply: 'إلغاء الرد',
        designateSpeaker: 'تعيين كمتحدث',
        reloadStream: 'إعادة تحميل البث',
        gamingMode: 'وضع الألعاب',
        lockResolution: 'قفل/إلغاء قفل الدقة',
        resizeClient: 'تغيير الحجم ليناسب العميل',
        invite: 'دعوة شخص ما إلى هذه الجلسة',
    },
    usernamePrompt: {
        title: 'أهلاً بك!',
        description: 'الرجاء اختيار اسم مستخدم للانضمام إلى الجلسة.',
        placeholder: 'اسمك',
        joinButton: 'انضمام',
    },
    chat: {
        inputPlaceholder: 'اكتب رسالة...',
        selfUsername: 'أنت',
        replyingTo: 'ردًا على <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> انضم إلى الغرفة.',
        userLeft: '<b>{username}</b> غادر الغرفة.',
        usernameChanged: '<b>{old_username}</b> يُعرف الآن باسم <b>{new_username}</b>.',
    },
    inviteLinks: {
        participant: 'رابط دعوة لاعب',
        readonly: 'رابط دعوة مشاهد',
        copied: 'تم نسخ الرابط',
        failed: 'تعذر إنشاء رابط الدعوة',
    },
    disconnect: {
        title: 'انقطع الاتصال',
        message: 'انتهت الجلسة.',
    },
    waiting: {
        title: 'المتحكم غائب',
        message: 'الجلسة نشطة. في انتظار استئناف البث من قبل المتحكم.',
    },
};

// Korean
const ko = {
    pageTitle: 'Webstation 협업',
    localUsername: '나',
    settings: {
        title: '설정',
        microphoneLabel: '마이크',
        webcamLabel: '웹캠',
    },
    alerts: {
        mediaAccessError: '카메라 또는 마이크에 액세스할 수 없습니다: {message}',
    },
    devices: {
        unlabeledDevice: '{kind} 장치 {number}',
    },
    tooltips: {
        toggleRemoteAudio: '오디오 음소거/음소거 해제',
        toggleRemoteVideo: '비디오 음소거/음소거 해제',
        toggleLocalMic: '마이크 음소거/음소거 해제',
        toggleLocalWebcam: '웹캠 시작/중지',
        toggleSessionAudio: '세션 오디오 음소거/음소거 해제',
        sessionVolume: '세션 볼륨',
        reply: '답장',
        cancelReply: '답장 취소',
        designateSpeaker: '발표자로 지정',
        reloadStream: '스트림 새로 고침',
        gamingMode: '게임 모드',
        lockResolution: '해상도 잠금/잠금 해제',
        resizeClient: '클라이언트에 맞게 크기 조정',
        invite: '이 세션에 누군가를 초대',
    },
    usernamePrompt: {
        title: '환영합니다!',
        description: '세션에 참여하려면 사용자 이름을 선택하세요.',
        placeholder: '이름',
        joinButton: '참여',
    },
    chat: {
        inputPlaceholder: '메시지를 입력하세요...',
        selfUsername: '나',
        replyingTo: '<b>{sender}</b>에게 답장 중',
    },
    systemMessages: {
        userJoined: '<b>{username}</b>님이 방에 참여했습니다.',
        userLeft: '<b>{username}</b>님이 방을 나갔습니다.',
        usernameChanged: '<b>{old_username}</b>님의 이름이 <b>{new_username}</b>(으)로 변경되었습니다.',
    },
    inviteLinks: {
        participant: '플레이어 초대 링크',
        readonly: '관전자 초대 링크',
        copied: '링크가 복사되었습니다',
        failed: '초대 링크를 생성할 수 없습니다',
    },
    disconnect: {
        title: '연결 끊김',
        message: '세션이 종료되었습니다.',
    },
    waiting: {
        title: '컨트롤러 부재 중',
        message: '세션이 활성 상태입니다. 컨트롤러가 스트림을 재개하기를 기다리는 중입니다.',
    },
};

// Japanese
const ja = {
    pageTitle: 'Webstation コラボレーション',
    localUsername: 'あなた',
    settings: {
        title: '設定',
        microphoneLabel: 'マイク',
        webcamLabel: 'ウェブカメラ',
    },
    alerts: {
        mediaAccessError: 'カメラまたはマイクにアクセスできませんでした：{message}',
    },
    devices: {
        unlabeledDevice: '{kind}デバイス{number}',
    },
    tooltips: {
        toggleRemoteAudio: '音声のミュート/ミュート解除',
        toggleRemoteVideo: 'ビデオのミュート/ミュート解除',
        toggleLocalMic: 'マイクのミュート/ミュート解除',
        toggleLocalWebcam: 'ウェブカメラの開始/停止',
        toggleSessionAudio: 'セッション音声のミュート/ミュート解除',
        sessionVolume: 'セッションの音量',
        reply: '返信',
        cancelReply: '返信をキャンセル',
        designateSpeaker: 'スピーカーに指定',
        reloadStream: 'ストリームを再読み込み',
        gamingMode: 'ゲーミングモード',
        lockResolution: '解像度をロック/ロック解除',
        resizeClient: 'クライアントに合わせてサイズ変更',
        invite: 'このセッションに誰かを招待',
    },
    usernamePrompt: {
        title: 'ようこそ！',
        description: 'セッションに参加するためのユーザー名を選択してください。',
        placeholder: 'あなたの名前',
        joinButton: '参加',
    },
    chat: {
        inputPlaceholder: 'メッセージを入力...',
        selfUsername: 'あなた',
        replyingTo: '<b>{sender}</b>に返信中',
    },
    systemMessages: {
        userJoined: '<b>{username}</b>がルームに参加しました。',
        userLeft: '<b>{username}</b>がルームを退出しました。',
        usernameChanged: '<b>{old_username}</b>は<b>{new_username}</b>に名前を変更しました。',
    },
    inviteLinks: {
        participant: 'プレイヤー招待リンク',
        readonly: '観戦者招待リンク',
        copied: 'リンクをコピーしました',
        failed: '招待リンクを作成できませんでした',
    },
    disconnect: {
        title: '切断されました',
        message: 'セッションは終了しました。',
    },
    waiting: {
        title: 'コントローラーが離席中',
        message: 'セッションはアクティブです。コントローラーがストリームを再開するのを待機しています。',
    },
};

// Vietnamese
const vi = {
    pageTitle: 'Hợp tác Webstation',
    localUsername: 'Bạn',
    settings: {
        title: 'Cài đặt',
        microphoneLabel: 'Micrô',
        webcamLabel: 'Webcam',
    },
    alerts: {
        mediaAccessError: 'Không thể truy cập máy ảnh hoặc micrô của bạn: {message}',
    },
    devices: {
        unlabeledDevice: 'Thiết bị {kind} {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'Tắt/Bật âm thanh',
        toggleRemoteVideo: 'Tắt/Bật video',
        toggleLocalMic: 'Tắt/Bật micrô',
        toggleLocalWebcam: 'Bắt đầu/Dừng webcam',
        toggleSessionAudio: 'Tắt/Bật âm thanh phiên',
        sessionVolume: 'Âm lượng phiên',
        reply: 'Trả lời',
        cancelReply: 'Hủy trả lời',
        designateSpeaker: 'Chỉ định làm người phát biểu',
        reloadStream: 'Tải lại luồng',
        gamingMode: 'Chế độ chơi game',
        lockResolution: 'Khóa/Mở khóa độ phân giải',
        resizeClient: 'Thay đổi kích thước theo máy khách',
        invite: 'Mời ai đó vào phiên này',
    },
    usernamePrompt: {
        title: 'Chào mừng!',
        description: 'Vui lòng chọn tên người dùng để tham gia phiên.',
        placeholder: 'Tên của bạn',
        joinButton: 'Tham gia',
    },
    chat: {
        inputPlaceholder: 'Nhập tin nhắn...',
        selfUsername: 'Bạn',
        replyingTo: 'Đang trả lời <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> đã tham gia phòng.',
        userLeft: '<b>{username}</b> đã rời phòng.',
        usernameChanged: '<b>{old_username}</b> bây giờ được biết đến với tên <b>{new_username}</b>.',
    },
    inviteLinks: {
        participant: 'Liên kết mời người chơi',
        readonly: 'Liên kết mời người xem',
        copied: 'Đã sao chép liên kết',
        failed: 'Không thể tạo liên kết mời',
    },
    disconnect: {
        title: 'Đã ngắt kết nối',
        message: 'Phiên đã kết thúc.',
    },
    waiting: {
        title: 'Người điều khiển vắng mặt',
        message: 'Phiên đang hoạt động. Đang chờ người điều khiển tiếp tục luồng.',
    },
};

// Thai
const th = {
    pageTitle: 'ความร่วมมือ Webstation',
    localUsername: 'คุณ',
    settings: {
        title: 'การตั้งค่า',
        microphoneLabel: 'ไมโครโฟน',
        webcamLabel: 'เว็บแคม',
    },
    alerts: {
        mediaAccessError: 'ไม่สามารถเข้าถึงกล้องหรือไมโครโฟนของคุณได้: {message}',
    },
    devices: {
        unlabeledDevice: 'อุปกรณ์ {kind} {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'ปิด/เปิดเสียง',
        toggleRemoteVideo: 'ปิด/เปิดวิดีโอ',
        toggleLocalMic: 'ปิด/เปิดไมโครโฟน',
        toggleLocalWebcam: 'เริ่ม/หยุดเว็บแคม',
        toggleSessionAudio: 'ปิด/เปิดเสียงเซสชัน',
        sessionVolume: 'ระดับเสียงเซสชัน',
        reply: 'ตอบกลับ',
        cancelReply: 'ยกเลิกการตอบกลับ',
        designateSpeaker: 'กำหนดเป็นผู้พูด',
        reloadStream: 'โหลดสตรีมใหม่',
        gamingMode: 'โหมดเกม',
        lockResolution: 'ล็อก/ปลดล็อกความละเอียด',
        resizeClient: 'ปรับขนาดตามไคลเอนต์',
        invite: 'เชิญใครสักคนเข้าร่วมเซสชันนี้',
    },
    usernamePrompt: {
        title: 'ยินดีต้อนรับ!',
        description: 'โปรดเลือกชื่อผู้ใช้เพื่อเข้าร่วมเซสชัน',
        placeholder: 'ชื่อของคุณ',
        joinButton: 'เข้าร่วม',
    },
    chat: {
        inputPlaceholder: 'พิมพ์ข้อความ...',
        selfUsername: 'คุณ',
        replyingTo: 'กำลังตอบกลับ <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> ได้เข้าร่วมห้องแล้ว',
        userLeft: '<b>{username}</b> ได้ออกจากห้องแล้ว',
        usernameChanged: '<b>{old_username}</b> ตอนนี้เป็นที่รู้จักในชื่อ <b>{new_username}</b>',
    },
    inviteLinks: {
        participant: 'ลิงก์เชิญผู้เล่น',
        readonly: 'ลิงก์เชิญผู้ชม',
        copied: 'คัดลอกลิงก์แล้ว',
        failed: 'ไม่สามารถสร้างลิงก์คำเชิญได้',
    },
    disconnect: {
        title: 'ตัดการเชื่อมต่อแล้ว',
        message: 'เซสชันสิ้นสุดลงแล้ว',
    },
    waiting: {
        title: 'ผู้ควบคุมไม่อยู่',
        message: 'เซสชันกำลังทำงาน กำลังรอให้ผู้ควบคุมดำเนินการสตรีมต่อ',
    },
};

// Filipino
const fil = {
    pageTitle: 'Pakikipagtulungan sa Webstation',
    localUsername: 'Ikaw',
    settings: {
        title: 'Mga Setting',
        microphoneLabel: 'Mikropono',
        webcamLabel: 'Webcam',
    },
    alerts: {
        mediaAccessError: 'Hindi ma-access ang iyong camera o mikropono: {message}',
    },
    devices: {
        unlabeledDevice: '{kind} device {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'I-mute/I-unmute ang Audio',
        toggleRemoteVideo: 'I-mute/I-unmute ang Video',
        toggleLocalMic: 'I-mute/I-unmute ang Mikropono',
        toggleLocalWebcam: 'Simulan/Itigil ang Webcam',
        toggleSessionAudio: 'I-mute/I-unmute ang Audio ng Session',
        sessionVolume: 'Volume ng Session',
        reply: 'Sumagot',
        cancelReply: 'Kanselahin ang Sagot',
        designateSpeaker: 'Italaga bilang Tagapagsalita',
        reloadStream: 'I-reload ang Stream',
        gamingMode: 'Gaming Mode',
        lockResolution: 'I-lock/I-unlock ang Resolusyon',
        resizeClient: 'I-resize sa Client',
        invite: 'Mag-imbita ng isang tao sa session na ito',
    },
    usernamePrompt: {
        title: 'Maligayang pagdating!',
        description: 'Mangyaring pumili ng username para sumali sa session.',
        placeholder: 'Iyong Pangalan',
        joinButton: 'Sumali',
    },
    chat: {
        inputPlaceholder: 'Mag-type ng mensahe...',
        selfUsername: 'Ikaw',
        replyingTo: 'Sumasagot kay <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: 'Si <b>{username}</b> ay sumali sa room.',
        userLeft: 'Si <b>{username}</b> ay umalis sa room.',
        usernameChanged: 'Si <b>{old_username}</b> ay kilala na ngayon bilang <b>{new_username}</b>.',
    },
    inviteLinks: {
        participant: 'Link ng Imbitasyon para sa Manlalaro',
        readonly: 'Link ng Imbitasyon para sa Manonood',
        copied: 'Nakopya ang link',
        failed: 'Hindi malikha ang invite link',
    },
    disconnect: {
        title: 'Nadiskonekta',
        message: 'Nagtapos na ang session.',
    },
    waiting: {
        title: 'Wala ang Controller',
        message: 'Aktibo ang session. Hinihintay ang controller na ipagpatuloy ang stream.',
    },
};

// Danish
const da = {
    pageTitle: 'Webstation Samarbejde',
    localUsername: 'Dig',
    settings: {
        title: 'Indstillinger',
        microphoneLabel: 'Mikrofon',
        webcamLabel: 'Webcam',
    },
    alerts: {
        mediaAccessError: 'Kunne ikke få adgang til dit kamera eller din mikrofon: {message}',
    },
    devices: {
        unlabeledDevice: '{kind}-enhed {number}',
    },
    tooltips: {
        toggleRemoteAudio: 'Slå lyd til/fra',
        toggleRemoteVideo: 'Slå video til/fra',
        toggleLocalMic: 'Slå mikrofon til/fra',
        toggleLocalWebcam: 'Start/Stop webcam',
        toggleSessionAudio: 'Slå sessionslyd til/fra',
        sessionVolume: 'Sessionslydstyrke',
        reply: 'Svar',
        cancelReply: 'Annuller svar',
        designateSpeaker: 'Udpeg som taler',
        reloadStream: 'Genindlæs stream',
        gamingMode: 'Spiltilstand',
        lockResolution: 'Lås/Lås op for opløsning',
        resizeClient: 'Tilpas størrelse til klient',
        invite: 'Inviter nogen til denne session',
    },
    usernamePrompt: {
        title: 'Velkommen!',
        description: 'Vælg venligst et brugernavn for at deltage i sessionen.',
        placeholder: 'Dit navn',
        joinButton: 'Deltag',
    },
    chat: {
        inputPlaceholder: 'Skriv en besked...',
        selfUsername: 'Dig',
        replyingTo: 'Svarer til <b>{sender}</b>',
    },
    systemMessages: {
        userJoined: '<b>{username}</b> er kommet ind i rummet.',
        userLeft: '<b>{username}</b> har forladt rummet.',
        usernameChanged: '<b>{old_username}</b> er nu kendt som <b>{new_username}</b>.',
    },
    inviteLinks: {
        participant: 'Invitationslink til spiller',
        readonly: 'Invitationslink til tilskuer',
        copied: 'Link kopieret',
        failed: 'Kunne ikke oprette et invitationslink',
    },
    disconnect: {
        title: 'Forbindelse afbrudt',
        message: 'Sessionen er afsluttet.',
    },
    waiting: {
        title: 'Controlleren er væk',
        message: 'Sessionen er aktiv. Venter på at controlleren genoptager streamen.',
    },
};


const translations = {
    en,
    es,
    zh,
    hi,
    pt,
    fr,
    ru,
    de,
    tr,
    it,
    nl,
    ar,
    ko,
    ja,
    vi,
    th,
    fil,
    da,
};

function getTranslator(langCode = 'en') {
    const baseLang = langCode.split('-')[0].toLowerCase();
    const langDict = translations[baseLang] || translations.en;
    const fallbackDict = translations.en;

    const t = (key, variables = {}) => {
        const keys = key.split('.');
        let value = keys.reduce((obj, k) => (obj && obj[k] !== undefined) ? obj[k] : undefined, langDict);

        if (value === undefined) {
            value = keys.reduce((obj, k) => (obj && obj[k] !== undefined) ? obj[k] : undefined, fallbackDict);
        }

        if (value === undefined) {
            console.warn(`Translation key not found: ${key}`);
            return key;
        }

        if (typeof value !== 'string') {
            return value;
        }
        
        let processedText = value.replace(/\{(\w+),\s*plural,\s*(.*)\}/g, (match, varName, rulesStr) => {
            if (!variables.hasOwnProperty(varName)) return match;
            const count = variables[varName];
            const rules = {};
            const ruleRegex = /(\w+)\s*\{((?:[^{}]|{[^{}]*})*)\}/g;
            let ruleMatch;
            while ((ruleMatch = ruleRegex.exec(rulesStr)) !== null) {
                rules[ruleMatch[1]] = ruleMatch[2];
            }
            let resultText;
            if (count === 1 && rules.one) resultText = rules.one;
            else if (rules.other) resultText = rules.other;
            else return match;
            return resultText;
        });

        for (const placeholder in variables) {
            const regex = new RegExp(`\\{${placeholder}\\}`, 'g');
            const substitution = String(variables[placeholder]);
            // A function replacement keeps $&, $` and $' inside a peer-chosen
            // value from being read as replacement patterns, which would splice
            // the surrounding markup into text destined for innerHTML.
            processedText = processedText.replace(regex, () => substitution);
        }
        
        return processedText;
    };

    return { t };
};

export { getTranslator };
