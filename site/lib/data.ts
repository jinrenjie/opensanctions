// import { join } from 'path'
// import { promises as fs } from 'fs';
import { IModelDatum } from "@alephdata/followthemoney"
import { IDataset, ICollection, ISource, IIssueIndex, IIndex, IIssue, IOpenSanctionsEntity, IDatasetDetails } from "./types";
import { API_URL, BASE_URL, INDEX_URL, ISSUES_URL } from "./constants";
import { markdownToHtml } from './util';

// const indexCache = join(process.cwd(), 'public', '_index.json')

async function fetchJsonUrl(url: string): Promise<any> {
  const data = await fetch(url)
  return await data.json()
}

export async function fetchIndex(): Promise<IIndex> {
  const data = await fetch(INDEX_URL, { cache: "force-cache" })
  const index = await data.json()
  index.details = {};
  index.datasets = index.datasets.map((raw: any) => {
    const { description, targets, resources, ...ds } = raw;
    const markdown = markdownToHtml(description)
    index.details[ds.name] = { description: markdown, targets, resources } as IDatasetDetails
    ds.link = `/datasets/${ds.name}/`
    ds.opensanctions_url = BASE_URL + ds.link

    return ds.type === 'collection' ? ds as ICollection : ds as ISource
  })
  index.model = index.model as IModelDatum
  return index as IIndex
}

export async function getDatasets(): Promise<Array<IDataset>> {
  const index = await fetchIndex()
  return index.datasets
}

export async function getDatasetByName(name: string): Promise<IDataset | undefined> {
  const datasets = await getDatasets()
  return datasets.find((dataset) => dataset.name === name)
}

export async function getDatasetDetails(name: string): Promise<IDatasetDetails | undefined> {
  const index = await fetchIndex()
  return index.details[name];
}

export async function getIssues(): Promise<Array<IIssue>> {
  // const index = await parseJsonFile('issues.json') as IIssueIndex;
  const index = await fetchJsonUrl(ISSUES_URL) as IIssueIndex;
  return index.issues
}

export async function getDatasetIssues(dataset?: IDataset): Promise<Array<IIssue>> {
  const issues = await getIssues()
  return issues.filter(issue => issue.dataset === dataset?.name);
}

export async function getEntityById(id: string): Promise<IOpenSanctionsEntity | null> {
  const url = `${API_URL}/entities/${id}`
  const data = await fetch(url)
  if (!data.ok) {
    // console.log('ERROR', data);
    return null;
  }
  return await data.json()
}
