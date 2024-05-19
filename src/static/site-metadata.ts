interface ISiteMetadataResult {
  siteTitle: string;
  siteUrl: string;
  description: string;
  keywords: string;
  logo: string;
  navLinks: {
    name: string;
    url: string;
  }[];
}

const data: ISiteMetadataResult = {
  siteTitle: 'Riding Page',
  siteUrl: 'https://github.com/vvanglro',
  logo: 'https://avatars.githubusercontent.com/u/43594924?v=4',
  description: 'Personal site and blog',
  keywords: 'workouts, running, cycling, riding, roadtrip, hiking, swimming',
  navLinks: [
    {
      name: 'Blog',
      url: 'https://github.com/vvanglro',
    },
    {
      name: 'About',
      url: 'https://github.com/vvanglro',
    },
  ],
};

export default data;
