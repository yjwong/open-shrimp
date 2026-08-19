import type { APIRoute, GetStaticPaths } from 'astro';
import { docPages, pageHeader, type DocPage } from '../../lib/llms';

export const prerender = true;

/**
 * `/llms/<id>.md` — the Markdown source of one documentation page.
 *
 * Serving the source rather than the rendered page keeps navigation,
 * search widgets and layout chrome out of what a reader gets back, so the
 * whole response is content.
 */
export const getStaticPaths: GetStaticPaths = async () => {
	const pages = await docPages();
	return pages.map((page) => ({
		params: { slug: page.id },
		props: { page },
	}));
};

export const GET: APIRoute = async ({ props }) => {
	const { page } = props as { page: DocPage };
	return new Response(`${pageHeader(page)}\n${page.body}`, {
		headers: { 'Content-Type': 'text/markdown; charset=utf-8' },
	});
};
