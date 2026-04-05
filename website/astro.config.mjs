// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// https://astro.build/config
export default defineConfig({
	site: 'https://shrimp.wong.place',
	integrations: [
		starlight({
			title: 'OpenShrimp',
			logo: { src: './src/assets/logo.svg' },
			customCss: ['./src/styles/custom.css'],
			social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/yjwong/open-shrimp' }],
			head: [
				{
					tag: 'meta',
					attrs: { property: 'og:image', content: 'https://shrimp.wong.place/og-image.png' },
				},
			],
			sidebar: [
				{
					label: 'Getting Started',
					items: [
						{ label: 'Installation', slug: 'getting-started/installation' },
						{ label: 'Telegram Setup', slug: 'getting-started/telegram-setup' },
						{ label: 'Configuration', slug: 'getting-started/configuration' },
						{ label: 'First Conversation', slug: 'getting-started/first-conversation' },
					],
				},
				{
					label: 'Guides',
					autogenerate: { directory: 'guides' },
				},
				{
					label: 'Reference',
					autogenerate: { directory: 'reference' },
				},
				{
					label: 'Deployment',
					autogenerate: { directory: 'deployment' },
				},
			],
		}),
	],
});
