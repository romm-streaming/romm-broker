import Link from 'next/link';

const links = [
  {
    href: '/docs',
    title: 'Overview',
    body: 'What the broker is, how a session flows, and where it sits next to RomM and the webstation container.',
  },
  {
    href: '/docs/container',
    title: 'Run the container',
    body: 'The linuxserver/webstation:romm image: compose file, volumes, GPU, and what happens at boot.',
  },
  {
    href: '/docs/api',
    title: 'REST API',
    body: 'Activate, join, save and load state, move state files and save archives, exit.',
  },
  {
    href: '/docs/developer',
    title: 'Developer',
    body: 'Dev mode, tests, adding an emulator, and the generated Python reference.',
  },
];

export default function HomePage() {
  return (
    <div className="flex flex-col flex-1 items-center justify-center px-6 py-16 text-center">
      <h1 className="text-3xl font-bold mb-3">romm-broker</h1>
      <p className="max-w-2xl text-fd-muted-foreground mb-10">
        Session broker and collaboration interface for the RomM webstation container. One play
        session at a time: a game is activated over REST, the emulator launches with its save data
        restored, and players land in a shared room around the stream.
      </p>
      <div className="grid gap-4 sm:grid-cols-2 max-w-3xl w-full text-left">
        {links.map((link) => (
          <Link
            key={link.href}
            href={link.href}
            className="rounded-lg border border-fd-border bg-fd-card p-4 transition-colors hover:bg-fd-accent"
          >
            <div className="font-medium mb-1">{link.title}</div>
            <div className="text-sm text-fd-muted-foreground">{link.body}</div>
          </Link>
        ))}
      </div>
    </div>
  );
}
