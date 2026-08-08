/* <sense-device> — three.js render of the Sense locket.
   Attributes: state = "connected" | "scanning" | "off", scale, mini
   Idle auto-rotate, drag to spin with inertia, LED behaviour per state. */
(() => {
  window.__senseVer = 7;
  const THREE_URL = 'https://esm.sh/three@0.160.0';

  function roundedRectShape(THREE, w, h, r) {
    const s = new THREE.Shape();
    const x = -w / 2, y = -h / 2;
    s.moveTo(x + r, y);
    s.lineTo(x + w - r, y);
    s.quadraticCurveTo(x + w, y, x + w, y + r);
    s.lineTo(x + w, y + h - r);
    s.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    s.lineTo(x + r, y + h);
    s.quadraticCurveTo(x, y + h, x, y + h - r);
    s.lineTo(x, y + r);
    s.quadraticCurveTo(x, y, x + r, y);
    return s;
  }

  function envTexture(THREE) {
    const c = document.createElement('canvas');
    c.width = 64; c.height = 256;
    const ctx = c.getContext('2d');
    const g = ctx.createLinearGradient(0, 0, 0, 256);
    g.addColorStop(0.00, '#ffffff');
    g.addColorStop(0.42, '#f3f1ee');
    g.addColorStop(0.55, '#d8d5d0');
    g.addColorStop(1.00, '#a9a6a1');
    ctx.fillStyle = g; ctx.fillRect(0, 0, 64, 256);
    // soft window highlight
    const h = ctx.createRadialGradient(32, 46, 2, 32, 46, 40);
    h.addColorStop(0, 'rgba(255,255,255,1)');
    h.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = h; ctx.fillRect(0, 0, 64, 120);
    const t = new THREE.CanvasTexture(c);
    t.mapping = THREE.EquirectangularReflectionMapping;
    return t;
  }

  function glowTexture(THREE) {
    const c = document.createElement('canvas');
    c.width = c.height = 128;
    const ctx = c.getContext('2d');
    const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
    g.addColorStop(0, 'rgba(255,255,255,0.42)');
    g.addColorStop(0.25, 'rgba(255,255,255,0.20)');
    g.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = g; ctx.fillRect(0, 0, 128, 128);
    return new THREE.CanvasTexture(c);
  }

  function shadowTexture(THREE) {
    const c = document.createElement('canvas');
    c.width = c.height = 256;
    const ctx = c.getContext('2d');
    const g = ctx.createRadialGradient(128, 128, 0, 128, 128, 128);
    g.addColorStop(0, 'rgba(28,26,24,0.42)');
    g.addColorStop(0.45, 'rgba(28,26,24,0.16)');
    g.addColorStop(1, 'rgba(28,26,24,0)');
    ctx.fillStyle = g; ctx.fillRect(0, 0, 256, 256);
    return new THREE.CanvasTexture(c);
  }

  const STATES = {
    connected: { led: 0x3fbf7f, glow: 0x54d896, speed: 0.30 },
    scanning:  { led: 0xe8813f, glow: 0xf59b5c, speed: 0.75 },
    off:       { led: 0x8d8b88, glow: 0x8d8b88, speed: 0.10 },
  };

  class SenseDevice extends HTMLElement {
    static get observedAttributes() { return ['state']; }

    connectedCallback() {
      if (this._booted) return;
      this._booted = true;
      this.style.display = 'block';
      this.style.width = '100%';
      this.style.height = '100%';
      this.style.position = 'relative';
      this.style.touchAction = 'none';
      this.style.cursor = 'grab';
      this._boot();
    }

    attributeChangedCallback(n, o, v) {
      if (n === 'state' && o !== null && o !== v) this._pop = 1;
    }

    disconnectedCallback() {
      this._dead = true;
      if (this._raf) cancelAnimationFrame(this._raf);
      if (this._ro) this._ro.disconnect();
      if (this._renderer) this._renderer.dispose();
    }

    get _cfg() { return STATES[this.getAttribute('state')] || STATES.connected; }

    async _boot() {
      let THREE;
      try { THREE = await import(/* @vite-ignore */ THREE_URL); }
      catch (e) { console.warn('sense-device: three.js failed to load', e); return; }
      if (this._dead) return;
      try { this._build(THREE); } catch (e) { console.error('sense-device: build failed', e); }
    }

    _build(THREE) {
      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(28, 1, 0.1, 100);
      camera.position.set(0, 0.12, 9.3);

      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true, powerPreference: 'high-performance' });
      renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.toneMapping = THREE.ACESFilmicToneMapping;
      renderer.toneMappingExposure = 1.06;
      renderer.domElement.style.cssText = 'width:100%;height:100%;display:block';
      this.appendChild(renderer.domElement);
      this._renderer = renderer;

      const pmrem = new THREE.PMREMGenerator(renderer);
      const env = pmrem.fromEquirectangular(envTexture(THREE)).texture;
      scene.environment = env;

      scene.add(new THREE.HemisphereLight(0xffffff, 0xdedbd6, 0.55));
      const key = new THREE.DirectionalLight(0xffffff, 1.5);
      key.position.set(2.6, 4.2, 4.2); scene.add(key);
      const rim = new THREE.DirectionalLight(0xfff2e6, 0.9);
      rim.position.set(-3.4, 1.2, -2.6); scene.add(rim);

      // ── body ──────────────────────────────────────────────
      const W = 1.62, H = 2.24, D = 0.30;
      const geo = new THREE.ExtrudeGeometry(roundedRectShape(THREE, W, H, 0.44), {
        depth: D, bevelEnabled: true, bevelThickness: 0.055, bevelSize: 0.055,
        bevelOffset: 0, bevelSegments: 8, curveSegments: 48,
      });
      geo.center();
      const body = new THREE.Mesh(geo, new THREE.MeshPhysicalMaterial({
        color: 0xf7f5f1, roughness: 0.42, metalness: 0.0,
        clearcoat: 0.55, clearcoatRoughness: 0.42, envMapIntensity: 0.9,
      }));

      const group = new THREE.Group();
      group.add(body);
      const FZ = D / 2 + 0.055;   // front surface

      const dark = new THREE.MeshStandardMaterial({ color: 0x232326, roughness: 0.82, metalness: 0.1 });
      const etch = new THREE.MeshPhysicalMaterial({ color: 0xa39d94, roughness: 0.85, clearcoat: 0.12 });

      // two mic ports
      const micGeo = new THREE.CylinderGeometry(0.042, 0.042, 0.03, 24);
      [-0.20, 0.20].forEach((x) => {
        const m = new THREE.Mesh(micGeo, dark);
        m.rotation.x = Math.PI / 2;
        m.position.set(x, 0.80, FZ - 0.006);
        group.add(m);
        const ring = new THREE.Mesh(new THREE.TorusGeometry(0.058, 0.011, 12, 40), etch);
        ring.position.set(x, 0.80, FZ + 0.002);
        group.add(ring);
      });

      // engraved emblem
      const emblem = new THREE.Mesh(new THREE.TorusGeometry(0.20, 0.017, 14, 72), etch);
      emblem.position.set(0, 0.02, FZ + 0.002);
      group.add(emblem);
      const emblemInner = new THREE.Mesh(new THREE.TorusGeometry(0.105, 0.015, 14, 56), etch);
      emblemInner.position.set(0, 0.02, FZ + 0.002);
      group.add(emblemInner);
      const dot = new THREE.Mesh(new THREE.CircleGeometry(0.034, 28), etch);
      dot.position.set(0, 0.02, FZ + 0.004);
      group.add(dot);

      // status LED
      const ledMat = new THREE.MeshBasicMaterial({ color: new THREE.Color(this._cfg.led), toneMapped: false });
      const led = new THREE.Mesh(new THREE.CircleGeometry(0.062, 32), ledMat);
      led.position.set(0, -0.80, FZ + 0.005);
      group.add(led);
      const ledRing = new THREE.Mesh(new THREE.TorusGeometry(0.072, 0.010, 12, 40),
        new THREE.MeshStandardMaterial({ color: 0x9c9891, roughness: 0.7 }));
      ledRing.position.set(0, -0.80, FZ + 0.003);
      group.add(ledRing);
      const halo = new THREE.Sprite(new THREE.SpriteMaterial({
        map: glowTexture(THREE), color: this._cfg.glow, transparent: true,
        depthWrite: false, toneMapped: false, opacity: 0.3,
      }));
      halo.scale.setScalar(0.5);
      halo.position.set(0, -0.80, FZ + 0.03);
      group.add(halo);

      // side button
      const btn = new THREE.Mesh(new THREE.CylinderGeometry(0.052, 0.052, 0.055, 28),
        new THREE.MeshPhysicalMaterial({ color: 0xeceae5, roughness: 0.5, clearcoat: 0.4 }));
      btn.rotation.z = Math.PI / 2;
      btn.position.set(W / 2 + 0.045, 0.42, 0);
      group.add(btn);

      // USB-C port on the bottom edge
      const port = new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.075, 0.13), dark);
      port.position.set(0, -(H / 2) - 0.012, 0);
      group.add(port);

      // contact shadow
      const shadow = new THREE.Mesh(
        new THREE.PlaneGeometry(3.6, 3.6),
        new THREE.MeshBasicMaterial({ map: shadowTexture(THREE), transparent: true, depthWrite: false })
      );
      shadow.rotation.x = -Math.PI / 2;
      shadow.position.set(0, -1.48, 0.1);
      scene.add(shadow);

      const pivot = new THREE.Group();
      pivot.add(group);
      scene.add(pivot);
      pivot.rotation.x = -0.10;
      pivot.rotation.y = -0.55;

      // ── interaction ───────────────────────────────────────
      let vel = 0, drag = false, lastX = 0, lastY = 0, idle = 0;
      const el = renderer.domElement;
      const down = (e) => { drag = true; idle = 0; lastX = e.clientX; lastY = e.clientY; this.style.cursor = 'grabbing'; el.setPointerCapture?.(e.pointerId); };
      const move = (e) => {
        if (!drag) return;
        const dx = e.clientX - lastX, dy = e.clientY - lastY;
        lastX = e.clientX; lastY = e.clientY;
        pivot.rotation.y += dx * 0.010;
        pivot.rotation.x = Math.max(-0.7, Math.min(0.7, pivot.rotation.x + dy * 0.006));
        vel = dx * 0.010;
      };
      const up = () => { drag = false; idle = 0; this.style.cursor = 'grab'; };
      el.addEventListener('pointerdown', down);
      el.addEventListener('pointermove', move);
      el.addEventListener('pointerup', up);
      el.addEventListener('pointercancel', up);
      el.addEventListener('pointerleave', up);

      // ── resize ────────────────────────────────────────────
      const resize = () => {
        const w = this.clientWidth || 320, h = this.clientHeight || 320;
        renderer.setSize(w, h, false);
        camera.aspect = w / h;
        camera.fov = w / h < 0.95 ? 30 : 26;
        camera.updateProjectionMatrix();
      };
      this._ro = new ResizeObserver(resize); this._ro.observe(this);
      resize();

      // ── loop ──────────────────────────────────────────────
      const clock = new THREE.Clock();
      const tick = () => {
        if (this._dead) return;
        this._raf = requestAnimationFrame(tick);
        const dt = Math.min(clock.getDelta(), 0.05);
        const t = clock.elapsedTime;
        const cfg = this._cfg;

        if (!drag) {
          idle = Math.min(1, idle + dt * 0.9);
          pivot.rotation.y += vel + cfg.speed * dt * idle;
          vel *= 0.94;
          pivot.rotation.x += (-0.10 - pivot.rotation.x) * dt * 1.4;
        }
        group.position.y = Math.sin(t * 1.05) * 0.045;
        shadow.material.opacity = 0.85 - Math.sin(t * 1.05) * 0.09;
        shadow.scale.setScalar(1 - Math.sin(t * 1.05) * 0.035);

        // connect / state-change pop
        if (this._pop > 0) {
          this._pop = Math.max(0, this._pop - dt * 1.6);
          const k = 1 + Math.sin(this._pop * Math.PI) * 0.075;
          group.scale.setScalar(k);
        } else if (group.scale.x !== 1) group.scale.setScalar(1);

        const st = this.getAttribute('state') || 'connected';
        let pulse = st === 'scanning'
          ? 0.35 + 0.65 * (0.5 + 0.5 * Math.sin(t * 6.0))
          : st === 'off' ? 0.08 : 0.62 + 0.38 * (0.5 + 0.5 * Math.sin(t * 1.7));
        ledMat.color.set(cfg.led).multiplyScalar(0.5 + pulse * 0.5);
        halo.material.color.set(cfg.glow);
        halo.material.opacity = 0.05 + pulse * 0.32;
        halo.scale.setScalar(0.44 + pulse * 0.16);

        renderer.render(scene, camera);
      };
      tick();
    }
  }

  if (!customElements.get('sense-device')) customElements.define('sense-device', SenseDevice);
})();
